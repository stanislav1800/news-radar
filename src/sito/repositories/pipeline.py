"""Состояние конвейера для администратора, пульс worker'а и чистка старых данных."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Heartbeat, Post, PostStatus, Source, Story

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
    """Задержка постов одного типа источников за последние сутки, в секундах."""

    source_type: str
    posts: int
    collect_median: float | None   # от публикации до сбора: опрос, источник, Telegram
    collect_p90: float | None
    process_median: float | None   # от сбора до ленты: очистка, вектор, склейка у нас
    process_p90: float | None
    process_max: float | None


# Публикация → сбор → в сюжете. Время публикации у некоторых лент бывает «будущим» или неточным —
# отрицательные задержки считаем нулём
_LAG_SQL = text("""
    SELECT source.type AS source_type, count(*) AS posts,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY greatest(0, extract(epoch FROM post.fetched_at - post.published_at)))
               AS collect_median,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY greatest(0, extract(epoch FROM post.fetched_at - post.published_at)))
               AS collect_p90,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM post.clustered_at - post.fetched_at))
               AS process_median,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM post.clustered_at - post.fetched_at))
               AS process_p90,
           max(extract(epoch FROM post.clustered_at - post.fetched_at)) AS process_max
    FROM post JOIN source ON source.id = post.source_id
    WHERE post.clustered_at > now() - interval '24 hours' AND post.published_at IS NOT NULL
    GROUP BY source.type ORDER BY source.type
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

    async def lag(self) -> list[Lag]:
        """Сколько идёт пост от публикации до ленты: у источника и у нас (если растёт — конвейер не успевает)."""
        return [Lag(**row._mapping) for row in await self.session.execute(_LAG_SQL)]

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

    async def purge_old_texts(self, before: datetime, limit: int) -> int:
        """Убрать тексты и векторы постов старше before (ТЗ: храним 90 дней). Заголовок, его леммы
        (поиск по старым сюжетам идёт по заголовкам) и ссылка остаются.

        Пачками по limit: одна огромная транзакция надолго заблокировала бы таблицу.
        """
        batch = (
            select(Post.id)
            .where(
                Post.fetched_at < before,
                (Post.raw_text.is_not(None)) | (Post.text.is_not(None)) | (Post.text_lemmas.is_not(None))
                | (Post.embedding.is_not(None)) | (Post.full_text.is_not(None)),
            )
            .limit(limit)
            .scalar_subquery()
        )
        result = await self.session.execute(
            update(Post).where(Post.id.in_(batch)).values(raw_text=None, text=None, text_lemmas=None, embedding=None, full_text=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount  # type: ignore[attr-defined, no-any-return]
