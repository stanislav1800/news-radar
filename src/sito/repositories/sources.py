from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Post, Source, SourceType, Story
from sito.repositories.stories import StoryRepository

ERROR_MAX_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class SourceRow:
    """Строка таблицы источников в админке."""

    id: int
    name: str
    url: str
    enabled: bool
    poll_interval: int
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    posts_24h: int


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, source_id: int) -> Source | None:
        return await self.session.get(Source, source_id)

    async def get_by_url(self, url: str) -> Source | None:
        return await self.session.scalar(select(Source).where(Source.url == url))

    async def list_all(self) -> list[Source]:
        return list(await self.session.scalars(select(Source).order_by(Source.id)))

    async def add(self, connector_id: int, type_: SourceType, name: str, url: str, poll_interval: int) -> Source:
        source = Source(connector_id=connector_id, type=type_, name=name, url=url, poll_interval=poll_interval)
        self.session.add(source)
        await self.session.flush()  # получить id до коммита
        return source

    async def list_with_stats(self, connector_id: int) -> list[SourceRow]:
        posts_24h = (
            select(func.count())
            .where(Post.source_id == Source.id, Post.fetched_at > func.now() - text("interval '24 hours'"))
            .scalar_subquery()
        )
        query = (
            select(
                Source.id, Source.name, Source.url, Source.enabled, Source.poll_interval,
                Source.last_success_at, Source.last_error, Source.last_error_at, posts_24h.label("posts_24h"),
            )
            .where(Source.connector_id == connector_id)
            .order_by(Source.name)
        )
        return [SourceRow(**row._mapping) for row in await self.session.execute(query)]

    async def get_row(self, source_id: int) -> SourceRow | None:
        source = await self.get(source_id)
        if source is None:
            return None
        rows = [r for r in await self.list_with_stats(source.connector_id) if r.id == source_id]
        return rows[0] if rows else None

    async def delete(self, source_id: int) -> bool:
        """Удалить источник вместе с его постами. Сюжеты, где были посты, пересчитываются, опустевшие удаляются."""
        story_ids = list(await self.session.scalars(
            select(Post.story_id).where(Post.source_id == source_id, Post.story_id.is_not(None)).distinct()
        ))
        result = await self.session.execute(delete(Source).where(Source.id == source_id).returning(Source.id))
        if result.scalar_one_or_none() is None:
            return False
        if story_ids:
            has_posts = select(Post.id).where(Post.story_id == Story.id).exists()
            await self.session.execute(delete(Story).where(Story.id.in_(story_ids), ~has_posts))
            stories = StoryRepository(self.session)
            for story_id in await self.session.scalars(select(Story.id).where(Story.id.in_(story_ids))):
                await stories.refresh(story_id)
        return True

    async def set_enabled(self, source_id: int, enabled: bool) -> bool:
        """Возвращает False, если источника нет."""
        result = await self.session.execute(
            update(Source).where(Source.id == source_id).values(enabled=enabled).returning(Source.id)
        )
        return result.scalar_one_or_none() is not None

    async def due_for_polling(self, types: Collection[SourceType], limit: int = 100) -> list[int]:
        """Включённые источники, у которых с последней попытки прошло poll_interval секунд.

        Попытка — это и успех, и ошибка: упавший сайт опрашивается с тем же интервалом,
        а не на каждом круге цикла.
        """
        last_attempt = func.greatest(Source.last_success_at, Source.last_error_at)  # NULL не учитывается
        next_poll = last_attempt + func.make_interval(0, 0, 0, 0, 0, 0, Source.poll_interval)
        result = await self.session.execute(
            select(Source.id)
            .where(
                Source.enabled,
                Source.type.in_(types),
                or_(last_attempt.is_(None), next_poll <= func.now()),
            )
            .order_by(last_attempt.asc().nulls_first())  # сначала новые и самые давние
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_success(self, source_id: int, etag: str | None, last_modified: str | None) -> None:
        await self.session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(
                etag=etag,
                last_modified=last_modified,
                last_success_at=func.now(),
                last_error=None,
                last_error_at=None,
            )
        )

    async def mark_failed(self, source_id: int, error: str) -> None:
        await self.session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(last_error=error[:ERROR_MAX_LENGTH], last_error_at=func.now())
        )
