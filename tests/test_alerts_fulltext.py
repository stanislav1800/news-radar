"""Список поломок для уведомлений и полный текст статей RSS."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, Source
from sito.services.alerts import Problem, alert_text, find_problems
from sito.services.fulltext import FullTextService
from tests.conftest import MakeSource
from tests.test_web_stories import Stories


@pytest.fixture
def stories(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> Stories:
    return Stories(session_factory, make_source)


ARTICLE = "<html><body><nav>Меню</nav><article><h1>Заголовок</h1>" + "<p>" + (
    "Шахтёры Кузбасса добыли рекордный объём угля за октябрь, сообщили в министерстве. " * 6
) + "</p></article><footer>Подвал</footer></body></html>"


async def test_problems_and_alert_text(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource,
) -> None:
    source_id = await make_source(name="ТАСС")
    async with session_factory() as session, session.begin():
        source = await session.get(Source, source_id)
        assert source is not None
        source.last_error, source.last_error_at = "HTTP 503", datetime.now(UTC)

    async with session_factory() as session:
        problems = await find_problems(session, datetime.now(UTC))

    assert [p.key for p in problems] == [f"failing:{source_id}"]
    text = alert_text(problems, [Problem("silent:1", "RSS «РБК» молчит")])
    assert "«ТАСС» не собирается: HTTP 503" in text and "Починилось" in text and "РБК" in text


async def test_full_text_of_rss_article(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories,
) -> None:
    await stories.add([("ТАСС", "Рекорд добычи угля", timedelta(hours=1))])

    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=ARTICLE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as client:
        result = await FullTextService(session_factory, client).process_batch()

    async with session_factory() as session:
        post = await session.scalar(select(Post))
    assert result.fetched == 1 and post is not None and post.full_text_at is not None
    assert post.full_text is not None and "рекордный объём угля" in post.full_text and "Меню" not in post.full_text
