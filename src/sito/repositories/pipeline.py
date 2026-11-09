"""Состояние конвейера для администратора, пульс worker'а и чистка старых данных."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import (
    ConnectorCommand,
    Heartbeat,
    Post,
    PostStatus,
    Source,
    Story,
    StoryMergeCheck,
)
from sito.models.post import PURGE_WHERE

# Посты, которые ещё идут по конвейеру: отставание считается по самому старому из них
IN_PROGRESS = (PostStatus.NEW, PostStatus.CLEANED, PostStatus.EMBEDDED)


@dataclass(frozen=True, slots=True)
class PipelineSummary:
    posts_24h: int
    stories_24h: int
    by_status: dict[str, int]
    oldest_pending_at: datetime | None  # самый старый пост, ещё не прошедший конвейер


@dataclass(frozen=True, slots=True)
class Lag:
    """Задержка постов одного типа источников, попавших в ленту за окно, в секундах. Только «живые» посты:
    догруженные после простоя (опубликованы до запуска worker'а) — отдельным числом caught_up, иначе после
    каждого перезапуска медиана показывает часы простоя, а не работу сбора и конвейера."""

    source_type: str
    posts: int
    caught_up: int                 # догружено после простоя
    collect_median: float | None   # от публикации до сбора: опрос, источник, Telegram
    collect_p90: float | None
    process_median: float | None   # от сбора до ленты: очистка, вектор, склейка у нас
    process_p90: float | None
    process_max: float | None


LAG_WINDOW = timedelta(hours=1)

# Публикация → сбор → в сюжете. Время публикации у некоторых лент бывает «будущим» или неточным —
# отрицательные задержки считаем нулём. Нет пульса worker'а (тесты, первый запуск) — все посты «живые»
_LAG_SQL = text("""
    WITH started AS (
        SELECT coalesce((SELECT started_at FROM heartbeat WHERE name = 'worker'), '-infinity') AS at
    ), recent AS (
        SELECT source.type AS source_type, post.published_at >= started.at AS live,
               greatest(0, extract(epoch FROM post.fetched_at - post.published_at)) AS collect,
               extract(epoch FROM post.clustered_at - post.fetched_at) AS process
        FROM post JOIN source ON source.id = post.source_id CROSS JOIN started
        WHERE post.clustered_at > now() - make_interval(secs => :window) AND post.published_at IS NOT NULL
    )
    SELECT source_type, count(*) FILTER (WHERE live) AS posts, count(*) FILTER (WHERE NOT live) AS caught_up,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY collect) FILTER (WHERE live) AS collect_median,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY collect) FILTER (WHERE live) AS collect_p90,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY process) FILTER (WHERE live) AS process_median,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY process) FILTER (WHERE live) AS process_p90,
           max(process) FILTER (WHERE live) AS process_max
    FROM recent GROUP BY source_type ORDER BY source_type
""")


@dataclass(frozen=True, slots=True)
class FailedPost:
    id: int
    title: str
    url: str
    source_name: str
    last_error: str | None
    failed_at: datetime


class PipelineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def summary(self) -> PipelineSummary:
        day_ago = func.now() - text("interval '24 hours'")
        posts_24h = await self.session.scalar(select(func.count()).where(Post.fetched_at > day_ago)) or 0
        # По времени первого поста: created_at после recluster у всех сюжетов свежий
        stories_24h = await self.session.scalar(select(func.count()).where(Story.started_at > day_ago)) or 0
        by_status = {
            str(status): count
            for status, count in await self.session.execute(select(Post.status, func.count()).group_by(Post.status))
        }
        oldest = await self.session.scalar(select(func.min(Post.fetched_at)).where(Post.status.in_(IN_PROGRESS)))
        return PipelineSummary(posts_24h, stories_24h, by_status, oldest)

    async def lag(self, window: timedelta = LAG_WINDOW) -> list[Lag]:
        """Сколько идёт пост от публикации до ленты: у источника и у нас (если растёт — конвейер не успевает)."""
        rows = await self.session.execute(_LAG_SQL, {"window": window.total_seconds()})
        return [Lag(**row._mapping) for row in rows]

    async def failed_posts(self, limit: int) -> list[FailedPost]:
        query = (
            select(
                Post.id, Post.title, Post.url, Source.name.label("source_name"), Post.last_error,
                Post.updated_at.label("failed_at"),
            )
            .join(Source, Source.id == Post.source_id)
            .where(Post.status == PostStatus.FAILED)
            .order_by(Post.updated_at.desc())
            .limit(limit)
        )
        return [FailedPost(**row._mapping) for row in await self.session.execute(query)]

    async def retry_failed(self, post_ids: Sequence[int] | None = None) -> int:
        """Вернуть застрявшие посты в начало конвейера. Шаги повторяемы: очистка и вектор пересчитаются."""
        stmt = (
            update(Post).where(Post.status == PostStatus.FAILED)
            .values(status=PostStatus.NEW, attempt=0, last_error=None)
        )
        if post_ids is not None:
            stmt = stmt.where(Post.id.in_(post_ids))
        result = await self.session.execute(stmt)
        return result.rowcount  # type: ignore[attr-defined, no-any-return]

    async def beat(self, name: str, started_at: datetime) -> None:
        stmt = insert(Heartbeat).values(name=name, started_at=started_at, beat_at=func.now())
        await self.session.execute(stmt.on_conflict_do_update(
            index_elements=["name"], set_={"started_at": stmt.excluded.started_at, "beat_at": stmt.excluded.beat_at},
        ))

    async def heartbeat(self, name: str) -> Heartbeat | None:
        return await self.session.get(Heartbeat, name)

    async def purge_service_rows(self, now: datetime) -> int:
        """Служебные записи, которые иначе копились бы вечно: проверенные пары для объединения сюжетов
        (спрашиваются только про сюжеты последних часов) и выполненные поручения подключениям."""
        checks = await self.session.execute(
            delete(StoryMergeCheck).where(StoryMergeCheck.checked_at < now - timedelta(days=7))
        )
        commands = await self.session.execute(
            delete(ConnectorCommand).where(
                ConnectorCommand.status != "pending", ConnectorCommand.created_at < now - timedelta(days=30),
            )
        )
        return checks.rowcount + commands.rowcount  # type: ignore[attr-defined, no-any-return]

    async def purge_old_texts(self, before: datetime, limit: int) -> int:
        """Убрать тексты и векторы постов старше before (ТЗ: храним 90 дней). Заголовок, его леммы
        (поиск по старым сюжетам идёт по заголовкам) и ссылка остаются.

        Пачками по limit: одна огромная транзакция надолго заблокировала бы таблицу.
        """
        batch = (
            select(Post.id)
            .where(
                Post.fetched_at < before,
                text(f"({PURGE_WHERE})"),  # тот же текст, что у частичного индекса ix_post_purge
            )
            .limit(limit)
            .scalar_subquery()
        )
        result = await self.session.execute(
            update(Post).where(Post.id.in_(batch)).values(raw_text=None, text=None, text_lemmas=None, embedding=None, full_text=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount  # type: ignore[attr-defined, no-any-return]
