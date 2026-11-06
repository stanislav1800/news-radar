"""Список поломок для уведомлений и полный текст статей RSS."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, Source
from sito.repositories.pipeline import PipelineRepository
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


async def test_stale_backup_is_a_problem(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        assert not await find_problems(session, datetime.now(UTC))  # отметки нет — сервиса копий нет
    async with session_factory() as session, session.begin():
        await PipelineRepository(session).beat("backup", datetime.now(UTC))

    async with session_factory() as session:
        fresh = await find_problems(session, datetime.now(UTC))
        stale = await find_problems(session, datetime.now(UTC) + timedelta(hours=27))

    assert fresh == [] and [p.key for p in stale] == ["backup-stale"]


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


def test_article_link_skips_socials_and_profiles() -> None:
    from sito.services.fulltext import article_link

    assert article_link(["https://t.me/rian_ru", "https://max.ru/ria", "https://vk.com/wall-1_2"]) is None
    assert article_link(["https://ria.ru/", "https://ria.ru/20261103/peskov-2128856778.html"]) == (
        "https://ria.ru/20261103/peskov-2128856778.html")
    assert article_link(["https://tass.ru/politika/28281561"]) == "https://tass.ru/politika/28281561"


async def test_full_text_of_short_telegram_post_by_link(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource,
) -> None:
    from sqlalchemy import insert

    from sito.models import PostStatus, SourceType

    channel = await make_source(name="РИА", type=SourceType.TELEGRAM)
    async with session_factory() as session, session.begin():
        for n, (text, links) in enumerate([
            ("Песков рассказал о встрече.", ["https://max.ru/ria", "https://ria.ru/20261103/peskov-1.html"]),
            ("Песков рассказал о встрече. " * 30, ["https://ria.ru/20261103/peskov-2.html"]),  # длинный — не нужно
        ]):
            await session.execute(insert(Post).values(
                source_id=channel, external_id=f"t{n}", url=f"https://t.me/ria/{n}", url_normalized=f"t.me/ria/{n}",
                title="Песков", text=text, links=links, content_hash=f"t{n}", status=PostStatus.CLEANED,
            ))
    asked: list[str] = []

    def page(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, html=ARTICLE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as client:
        await FullTextService(session_factory, client).process_batch()

    async with session_factory() as session:
        posts = {p.external_id: p for p in await session.scalars(select(Post))}
    assert asked == ["https://ria.ru/20261103/peskov-1.html"]
    assert "рекордный объём угля" in (posts["t0"].full_text or "") and posts["t1"].full_text_at is None


async def test_full_text_skips_files_and_huge_pages(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories,
) -> None:
    await stories.add([("ТАСС", "Видео", timedelta(hours=1))], url="https://news.example/video.mp4")
    await stories.add([("РИА", "Огромная", timedelta(hours=1))], url="https://news.example/huge")

    def page(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".mp4"):
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"x" * 10)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>" + b"a" * 4_000_000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as client:
        result = await FullTextService(session_factory, client).process_batch()

    assert result.fetched == 0 and result.failed == 2  # ни видео, ни 4 МБ не читаются в память


async def test_rss_html_stub_is_an_error() -> None:
    from sito.models import Source
    from sito.services.collector.rss import RssCollector

    def stub(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><body>Checking your browser…</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(stub)) as client:
        with pytest.raises(ValueError, match="не лента"):
            await RssCollector(client).fetch(Source(id=1, url="https://feed.example/rss", state={}))
