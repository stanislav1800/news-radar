"""Издания: ТАСС из RSS и Telegram-канал ТАСС — одно издание в сюжете и одна строка в хронологии."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus, Source
from sito.repositories.sources import SourceRepository
from sito.repositories.stories import StoryRepository
from tests.conftest import MakeSource


async def test_channel_of_rss_outlet_counts_once(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource,
) -> None:
    rss = await make_source(name="ТАСС")
    async with session_factory() as session, session.begin():
        connector_id = (await session.get(Source, rss)).connector_id  # type: ignore[union-attr]
        channel = Source(connector_id=connector_id, type="telegram", name="TACC", external_id="1", state={})
        session.add(channel)
        await session.flush()
        story = await StoryRepository(session).create("Новость", datetime.now(UTC) - timedelta(hours=1))
        for n in range(6):  # статьи ТАСС и посты канала со ссылками на них
            url = f"https://tass.ru/politika/{n}"
            session.add(Post(source_id=rss, external_id=f"r{n}", url=url, url_normalized=url, title="Статья",
                             content_hash=f"r{n}", status=PostStatus.CLUSTERED, story_id=story.id if n == 0 else None))
            session.add(Post(source_id=channel.id, external_id=f"t{n}", url=f"https://t.me/tass_agency/{n}",
                             url_normalized=f"https://t.me/tass_agency/{n}", title="Пост", content_hash=f"t{n}",
                             links=[url], status=PostStatus.CLUSTERED, story_id=story.id if n == 0 else None))
        await session.flush()
        await StoryRepository(session).refresh(story.id)
        await session.refresh(story)
        assert story.sources_count == 2

        assert await SourceRepository(session).detect_outlets() == 2
        await StoryRepository(session).recount_outlets()
        await session.refresh(story)

        assert story.sources_count == 1
        assert [p.source_name for p in await StoryRepository(session).timeline(story.id)] == ["ТАСС"]
