"""IngestService с фальшивым коллектором: вся логика сохранения без сети."""

from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, Source, SourceType
from sito.services.collector.base import BaseCollector, FetchResult, RawPost
from sito.services.ingest import IngestService
from tests.conftest import MakeSource


@dataclass
class FakeCollector(BaseCollector):
    """Отдаёт заранее заданные посты: (external_id, url, title)."""

    items: list[tuple[str, str, str]] = field(default_factory=list)
    not_modified: bool = False
    error: Exception | None = None
    calls: int = 0

    async def fetch(self, source: Source) -> FetchResult:
        self.calls += 1
        if self.error:
            raise self.error
        if self.not_modified:
            return FetchResult(not_modified=True, etag=source.etag)
        posts = [RawPost(source.id, eid, url, None, title, f"<p>{title}</p>") for eid, url, title in self.items]
        return FetchResult(posts=posts, etag='"v1"')


def make_service(factory: async_sessionmaker[AsyncSession], collector: FakeCollector) -> IngestService:
    return IngestService(factory, {SourceType.RSS: collector})


async def count_posts(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(Post))).scalar_one()


async def get_source(factory: async_sessionmaker[AsyncSession], source_id: int) -> Source:
    async with factory() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        return source


async def test_saves_posts_and_marks_success(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    collector = FakeCollector(items=[
        ("1", "https://news.example/1?utm_source=rss", "Первый"),
        ("2", "https://news.example/2", "Второй"),
    ])

    result = await make_service(session_factory, collector).ingest_source(source_id)

    assert (result.fetched, result.inserted) == (2, 2)
    assert await count_posts(session_factory) == 2
    source = await get_source(session_factory, source_id)
    assert source.etag == '"v1"'
    assert source.last_success_at is not None
    async with session_factory() as session:
        urls = set((await session.execute(select(Post.url_normalized))).scalars())
    assert urls == {"https://news.example/1", "https://news.example/2"}


async def test_repeat_poll_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    service = make_service(session_factory, FakeCollector(items=[("1", "https://news.example/1", "Первый")]))
    await service.ingest_source(source_id)

    result = await service.ingest_source(source_id)

    assert (result.inserted, result.updated, result.unchanged) == (0, 0, 1)


async def test_same_article_from_other_source_is_duplicate(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    first, second = await make_source(), await make_source()
    await make_service(
        session_factory, FakeCollector(items=[("a", "https://news.example/story", "Новость")])
    ).ingest_source(first)

    # Другой источник с другим id поста, но та же статья (адрес отличается только меткой)
    result = await make_service(session_factory, FakeCollector(items=[
        ("b", "https://www.news.example/story/?utm_source=tg", "Новость"),
        ("c", "https://news.example/other", "Другая"),
    ])).ingest_source(second)

    assert (result.duplicates, result.inserted) == (1, 1)
    assert await count_posts(session_factory) == 2


async def test_duplicate_urls_inside_batch(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    collector = FakeCollector(items=[
        ("1", "https://news.example/story", "Новость"),
        ("2", "https://news.example/story#comments", "Та же новость"),
    ])

    result = await make_service(session_factory, collector).ingest_source(source_id)

    assert (result.duplicates, result.inserted) == (1, 1)


async def test_invalid_urls_are_counted_and_skipped(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    collector = FakeCollector(items=[("1", "/relative", "Плохой"), ("2", "https://news.example/2", "Хороший")])

    result = await make_service(session_factory, collector).ingest_source(source_id)

    assert (result.invalid, result.inserted) == (1, 1)


async def test_not_modified(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> None:
    source_id = await make_source(etag='"v0"')

    result = await make_service(session_factory, FakeCollector(not_modified=True)).ingest_source(source_id)

    assert result.not_modified
    assert await count_posts(session_factory) == 0
    assert (await get_source(session_factory, source_id)).last_success_at is not None


async def test_error_is_recorded_and_reraised(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    service = make_service(session_factory, FakeCollector(error=ConnectionError("сайт недоступен")))

    with pytest.raises(ConnectionError):
        await service.ingest_source(source_id)

    source = await get_source(session_factory, source_id)
    assert source.last_error == "ConnectionError: сайт недоступен"
    assert source.last_error_at is not None
    assert source.last_success_at is None


async def test_disabled_source_is_skipped(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source(enabled=False)
    collector = FakeCollector(items=[("1", "https://news.example/1", "Первый")])

    result = await make_service(session_factory, collector).ingest_source(source_id)

    assert result.skipped
    assert collector.calls == 0


async def test_unknown_source(session_factory: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(LookupError):
        await make_service(session_factory, FakeCollector()).ingest_source(999_999)
