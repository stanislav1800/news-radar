from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ScalarSelect, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Post, Source, SourceType, Story
from sito.repositories.stories import StoryRepository

ERROR_MAX_LENGTH = 2000


# «Источник молчит»: новых постов нет дольше, чем в десять раз его обычного промежутка между постами
# (по истории за неделю), но не меньше 6 часов и не больше 3 суток. ТАСС пишет раз в несколько минут —
# тревога через 6 часов; OpenNET пару раз в день — через 3 суток
SILENCE_FACTOR = 10
SILENCE_MIN = timedelta(hours=6)
SILENCE_MAX = timedelta(days=3)
HISTORY = timedelta(days=7)
MIN_POLL_SECONDS = 60
LATE_MINUTES = 60  # RSS, у которой новости в среднем приходят позже, — «с опозданием»  # чаще раза в минуту ленты не опрашиваются


def silence_limit(posts_week: int) -> timedelta:
    usual_gap = HISTORY / max(posts_week, 1)
    return min(max(usual_gap * SILENCE_FACTOR, SILENCE_MIN), SILENCE_MAX)


@dataclass(frozen=True, slots=True)
class SourceRow:
    """Строка таблицы источников в админке."""

    id: int
    name: str
    url: str | None
    external_id: str | None
    enabled: bool
    poll_interval: int | None
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    failures: int
    created_at: datetime
    last_post_at: datetime | None  # когда пришёл последний новый пост
    posts_24h: int
    posts_week: int
    info: dict[str, Any] = field(default_factory=dict)  # что рассказал сервис (подписчики канала и т. п.)
    type: str = "rss"
    # Медиана задержки «опубликовано → собрано» за сутки, минут: лента отдаёт новости поздно
    delay_minutes: float | None = None

    @property
    def late(self) -> bool:
        return self.type == "rss" and self.delay_minutes is not None and self.delay_minutes > LATE_MINUTES

    @property
    def failing(self) -> bool:
        """Последняя попытка сбора — ошибка."""
        return bool(
            self.enabled and self.last_error and self.last_error_at
            and (self.last_success_at is None or self.last_error_at > self.last_success_at)
        )

    def silent_since(self, now: datetime) -> datetime | None:
        """С какого момента источник молчит дольше нормы для него; None — всё в порядке.

        Источник с ошибкой не «молчит»: у него своя причина, она показывается отдельно.
        """
        if not self.enabled or self.failing:
            return None
        since = self.last_post_at or self.created_at
        return since if now - since > silence_limit(self.posts_week) else None


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, source_id: int) -> Source | None:
        return await self.session.get(Source, source_id)

    async def get_by_url(self, url: str) -> Source | None:
        return await self.session.scalar(select(Source).where(Source.url == url))

    async def get_by_external_id(self, type_: SourceType, external_id: str) -> Source | None:
        return await self.session.scalar(
            select(Source).where(Source.type == type_, Source.external_id == external_id)
        )

    async def list_all(self) -> list[Source]:
        return list(await self.session.scalars(select(Source).order_by(Source.id)))

    async def list_for_connector(self, connector_id: int) -> list[Source]:
        return list(await self.session.scalars(
            select(Source).where(Source.connector_id == connector_id).order_by(Source.id)
        ))

    async def add(
        self, connector_id: int, type_: SourceType, name: str, *,
        url: str | None = None, external_id: str | None = None, poll_interval: int | None = None,
    ) -> Source:
        source = Source(
            connector_id=connector_id, type=type_, name=name, url=url, external_id=external_id,
            poll_interval=poll_interval, state={},
        )
        self.session.add(source)
        await self.session.flush()  # получить id до коммита
        return source

    async def get_or_add_external(
        self, connector_id: int, type_: SourceType, external_id: str, name: str, url: str | None,
        *, enabled: bool = True,
    ) -> Source:
        """Источник слушающего сборщика: найти по id у сервиса или завести.

        Гонка двух подключений за один новый канал решается уникальностью (type, external_id):
        второе получит уже заведённый источник.
        """
        if url is not None:
            same_url = await self.get_by_url(url)
            if same_url is not None and same_url.external_id is None:
                # Канал добавили ссылкой, а пост из него пришёл раньше подписки Сито: это он и есть
                same_url.external_id = external_id
                same_url.name = name
                await self.session.flush()
                return same_url
            if same_url is not None and same_url.external_id != external_id:
                url = None  # адрес занят другим каналом (имя передали): заводим без адреса
        stmt = (
            insert(Source)
            .values(connector_id=connector_id, type=type_, name=name, url=url, external_id=external_id, state={},
                    enabled=enabled)
            .on_conflict_do_nothing(index_elements=["type", "external_id"])
        )
        await self.session.execute(stmt)
        source = await self.get_by_external_id(type_, external_id)
        assert source is not None
        return source

    async def pending(self, connector_id: int) -> list[Source]:
        """Источники, добавленные ссылкой, на которые подключение ещё не подписалось и не споткнулось."""
        return list(await self.session.scalars(
            select(Source).where(
                Source.connector_id == connector_id, Source.external_id.is_(None), Source.url.is_not(None),
                Source.last_error.is_(None), Source.enabled,
            ).order_by(Source.id)
        ))

    async def bind_pending(self, source_id: int, external_id: str, name: str) -> Source | None:
        """Источнику по ссылке — id у сервиса. Если такой источник уже есть, ссылка удаляется: дубля нет."""
        pending = await self.get(source_id)
        if pending is None:
            return None
        existing = await self.get_by_external_id(pending.type, external_id)
        if existing is not None:
            await self.session.delete(pending)
            return existing
        pending.external_id = external_id
        pending.name = name
        await self.session.flush()
        return pending

    async def detect_outlets(self) -> int:
        """Определить издание у источников по ссылкам их постов за неделю. Возвращает число изменённых.

        RSS-лента — сайт, откуда её статьи (tass.ru). Telegram-канал — то же издание, если больше половины
        его постов со ссылками ведут на сайт одной из RSS-лент: так канал ТАСС узнаётся как ТАСС.
        Только сайты RSS-лент: иначе каналы, которые все ссылаются на youtube.com, стали бы «одним изданием».
        """
        result = await self.session.execute(text(r"""
            WITH rss AS (
                SELECT DISTINCT ON (post.source_id) post.source_id,
                       regexp_replace(split_part(split_part(post.url_normalized, '://', 2), '/', 1), '^www\.', '') AS host
                FROM post JOIN source ON source.id = post.source_id
                WHERE source.type = 'rss' AND post.fetched_at > now() - interval '7 days'
                GROUP BY post.source_id, 2 ORDER BY post.source_id, count(*) DESC
            ),
            linked AS (
                SELECT post.source_id,
                       regexp_replace(split_part(split_part(link, '://', 2), '/', 1), '^www\.', '') AS host,
                       count(DISTINCT post.id) AS posts
                FROM post JOIN source ON source.id = post.source_id, unnest(post.links) AS link
                WHERE source.type <> 'rss' AND post.fetched_at > now() - interval '7 days'
                GROUP BY 1, 2
            ),
            with_links AS (
                SELECT post.source_id, count(*) AS posts FROM post JOIN source ON source.id = post.source_id
                WHERE source.type <> 'rss' AND post.fetched_at > now() - interval '7 days'
                  AND cardinality(post.links) > 0
                GROUP BY 1
            ),
            channel AS (
                SELECT DISTINCT ON (linked.source_id) linked.source_id, linked.host
                FROM linked JOIN with_links USING (source_id)
                WHERE linked.host IN (SELECT host FROM rss) AND linked.posts >= 5
                  AND linked.posts * 2 > with_links.posts
                ORDER BY linked.source_id, linked.posts DESC
            ),
            found AS (SELECT source_id, host FROM rss UNION ALL SELECT source_id, host FROM channel)
            UPDATE source SET outlet = found.host FROM found
            WHERE source.id = found.source_id AND source.outlet IS DISTINCT FROM found.host
        """))
        return result.rowcount  # type: ignore[attr-defined, no-any-return]

    async def update_info(self, source_id: int, info: Mapping[str, Any]) -> None:
        """Дополнить сведения об источнике (ключи из info заменяются, остальные остаются)."""
        await self.session.execute(
            update(Source).where(Source.id == source_id)
            .values(info=Source.info.op("||")(cast(dict(info), JSONB)))
        )

    async def list_with_stats(self, connector_id: int | None = None) -> list[SourceRow]:
        """Источники со статистикой: одного подключения или все."""
        def posts_since(interval: str) -> ScalarSelect[int]:
            return (
                select(func.count())
                .where(Post.source_id == Source.id, Post.fetched_at > func.now() - text(f"interval '{interval}'"))
                .scalar_subquery()
            )
        last_post_at = select(func.max(Post.fetched_at)).where(Post.source_id == Source.id).scalar_subquery()
        delay = (
            select(func.percentile_cont(0.5).within_group(
                func.extract("epoch", Post.fetched_at - Post.published_at) / 60))
            .where(Post.source_id == Source.id, Post.fetched_at > func.now() - text("interval '24 hours'"),
                   Post.published_at <= Post.fetched_at)
            .scalar_subquery()
        )
        query = (
            select(
                Source.id, Source.name, Source.url, Source.external_id, Source.enabled, Source.poll_interval,
                Source.last_success_at, Source.last_error, Source.last_error_at, Source.failures,
                Source.created_at, last_post_at.label("last_post_at"),
                posts_since("24 hours").label("posts_24h"), posts_since("7 days").label("posts_week"), Source.info,
                Source.type, delay.label("delay_minutes"),
            )
            .order_by(Source.name)
        )
        if connector_id is not None:
            query = query.where(Source.connector_id == connector_id)
        return [SourceRow(**row._mapping) for row in await self.session.execute(query)]

    async def get_row(self, source_id: int) -> SourceRow | None:
        source = await self.get(source_id)
        if source is None:
            return None
        rows = [r for r in await self.list_with_stats(source.connector_id) if r.id == source_id]
        return rows[0] if rows else None

    async def delete_posts(self, source_id: int, external_ids: Collection[str]) -> int:
        """Убрать посты, которые источник удалил (канал удалил пост). Сюжеты пересчитываются."""
        story_ids = list(await self.session.scalars(
            select(Post.story_id).where(Post.source_id == source_id, Post.external_id.in_(external_ids),
                                        Post.story_id.is_not(None)).distinct()
        ))
        result = await self.session.execute(
            delete(Post).where(Post.source_id == source_id, Post.external_id.in_(external_ids))
        )
        await self._refresh_stories(story_ids)
        return result.rowcount  # type: ignore[attr-defined, no-any-return]

    async def _refresh_stories(self, story_ids: Collection[int | None]) -> None:
        if not story_ids:
            return
        has_posts = select(Post.id).where(Post.story_id == Story.id).exists()
        await self.session.execute(delete(Story).where(Story.id.in_(story_ids), ~has_posts))
        stories = StoryRepository(self.session)
        for story_id in await self.session.scalars(select(Story.id).where(Story.id.in_(story_ids))):
            await stories.refresh(story_id)

    async def rename(self, source_id: int, name: str) -> bool:
        result = await self.session.execute(
            update(Source).where(Source.id == source_id).values(name=name).returning(Source.id)
        )
        return result.scalar_one_or_none() is not None

    async def delete(self, source_id: int) -> bool:
        """Удалить источник вместе с его постами. Сюжеты, где были посты, пересчитываются, опустевшие удаляются."""
        story_ids = list(await self.session.scalars(
            select(Post.story_id).where(Post.source_id == source_id, Post.story_id.is_not(None)).distinct()
        ))
        result = await self.session.execute(delete(Source).where(Source.id == source_id).returning(Source.id))
        if result.scalar_one_or_none() is None:
            return False
        await self._refresh_stories(story_ids)
        return True

    async def set_enabled(self, source_id: int, enabled: bool) -> bool:
        """Возвращает False, если источника нет."""
        result = await self.session.execute(
            update(Source).where(Source.id == source_id).values(enabled=enabled).returning(Source.id)
        )
        return result.scalar_one_or_none() is not None

    async def due_for_polling(
        self, types: Collection[SourceType], limit: int = 100, max_backoff: timedelta = timedelta(hours=6),
    ) -> list[int]:
        """Включённые источники, которым пора на опрос.

        Пауза после последней попытки — poll_interval, а после неудач подряд она удваивается
        (1, 2, 4, 8… интервала), но не дольше max_backoff: сломанный сайт не опрашивается
        каждые 5 минут, а починившийся снова подхватывается сам.
        """
        last_attempt = func.greatest(Source.last_success_at, Source.last_error_at)  # NULL не учитывается
        # Активная лента опрашивается чаще: раз в половину обычного промежутка между её постами за неделю,
        # но не чаще раза в минуту и не реже poll_interval. У ТАСС новости выходят каждые пару минут
        posts_week = (
            select(func.count()).where(Post.source_id == Source.id, Post.fetched_at > func.now() - text("interval '7 days'"))
            .scalar_subquery()
        )
        interval = func.least(
            Source.poll_interval,
            func.greatest(MIN_POLL_SECONDS, HISTORY.total_seconds() / func.greatest(posts_week, 1) / 2),
        )
        pause = func.least(
            interval * func.power(2, func.greatest(Source.failures - 1, 0)),
            max_backoff.total_seconds(),
        )
        next_poll = last_attempt + func.make_interval(0, 0, 0, 0, 0, 0, pause)
        result = await self.session.execute(
            select(Source.id)
            .where(
                Source.enabled,
                Source.type.in_(types),
                Source.poll_interval.is_not(None),
                or_(last_attempt.is_(None), next_poll <= func.now()),
            )
            .order_by(last_attempt.asc().nulls_first())  # сначала новые и самые давние
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_success(self, source_id: int, state: Mapping[str, Any] | None = None) -> None:
        """Сбор прошёл. state — новый курсор сборщика; None — оставить прежний."""
        values: dict[str, Any] = {
            "last_success_at": func.now(), "last_error": None, "last_error_at": None, "failures": 0,
        }
        if state is not None:
            values["state"] = dict(state)
        await self.session.execute(update(Source).where(Source.id == source_id).values(**values))

    async def mark_failed(self, source_id: int, error: str) -> None:
        await self.session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(last_error=error[:ERROR_MAX_LENGTH], last_error_at=func.now(), failures=Source.failures + 1)
        )
