"""Сводка конвейера, повтор застрявших постов, пульс worker'а, чистка старых текстов."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus, Story
from sito.repositories.pipeline import PipelineRepository
from sito.repositories.sources import SourceRepository
from sito.repositories.stories import StoryRepository
from sito.services.users import UserService
from tests.conftest import MakeSource
from tests.test_web_stories import Stories, vec
from tests.web import PASSWORD, csrf_from, login


async def add_post(
    factory: async_sessionmaker[AsyncSession], source_id: int, n: int, status: PostStatus, **fields: object
) -> int:
    async with factory() as session, session.begin():
        return (await session.execute(insert(Post).values(
            source_id=source_id, external_id=f"id-{n}", url=f"https://news.example/{n}",
            url_normalized=f"https://news.example/{n}", title=f"Пост {n}", content_hash=f"h{n}",
            status=status, **fields,
        ).returning(Post.id))).scalar_one()


async def test_summary_and_retry(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> None:
    source = await make_source(name="ТАСС")
    await add_post(session_factory, source, 1, PostStatus.NEW)
    await add_post(session_factory, source, 2, PostStatus.CLUSTERED)
    failed = await add_post(session_factory, source, 3, PostStatus.FAILED, attempt=3, last_error="ValueError: битый HTML")

    async with session_factory() as session:
        repo = PipelineRepository(session)
        summary = await repo.summary()
        [stuck] = await repo.failed_posts(10)
    assert summary.posts_24h == 3
    assert summary.by_status == {"new": 1, "clustered": 1, "failed": 1}
    assert summary.oldest_pending_at is not None
    assert (stuck.id, stuck.source_name, stuck.last_error) == (failed, "ТАСС", "ValueError: битый HTML")

    async with session_factory() as session, session.begin():
        assert await PipelineRepository(session).retry_failed([failed]) == 1
    async with session_factory() as session:
        post = await session.get(Post, failed)
    assert post is not None and (post.status, post.attempt, post.last_error) == (PostStatus.NEW, 0, None)


async def test_purge_keeps_titles_and_stories(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """Старым постам — без текста и вектора; сюжет сохраняет центр, а recluster его не удаляет."""
    stories = Stories(session_factory, make_source)
    old_story = await stories.add([("ТАСС", "Старая новость", timedelta(days=100))], embedding=vec(1))
    fresh_story = await stories.add([("РБК", "Свежая новость", timedelta(hours=1))], embedding=vec(2))
    async with session_factory() as session, session.begin():
        # fetched_at по умолчанию — сейчас; делаем пост старым
        await session.execute(update(Post).where(Post.story_id == old_story)
                              .values(fetched_at=datetime.now(UTC) - timedelta(days=100)))

    async with session_factory() as session, session.begin():
        purged = await PipelineRepository(session).purge_old_texts(datetime.now(UTC) - timedelta(days=90), 100)
    assert purged == 1

    async with session_factory() as session, session.begin():
        [post] = await session.scalars(select(Post).where(Post.story_id == old_story))
        assert post.title == "Старая новость" and post.text is None and post.embedding is None
        await StoryRepository(session).refresh(old_story)
        story = await session.get(Story, old_story)
        assert story is not None and story.centroid is not None  # центр остался прежним

    async with session_factory() as session, session.begin():
        assert await StoryRepository(session).reset_all() == 1  # пересклеить можно только свежий пост
    async with session_factory() as session:
        assert await session.get(Story, old_story) is not None
        assert await session.get(Story, fresh_story) is None


# --- Веб ---------------------------------------------------------------------------------------

@pytest.fixture
async def admin(client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    async with session_factory() as session, session.begin():
        await UserService(session).create("boss", PASSWORD, is_admin=True)
    assert (await login(client, "boss")).status_code == 303
    return client


async def test_health(client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    response = await client.get("/health")  # без входа
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "worker": "unknown"}

    async with session_factory() as session, session.begin():
        await PipelineRepository(session).beat("worker", datetime.now(UTC))
    assert (await client.get("/health")).json()["worker"] == "ok"


async def test_summary_page(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    make_source: MakeSource,
) -> None:
    broken = await make_source(name="Известия")
    await make_source(name="Заглохшая лента", created_at=datetime.now(UTC) - timedelta(days=5))
    async with session_factory() as session, session.begin():
        await SourceRepository(session).mark_failed(broken, "HTTPStatusError: 429 Too Many Requests")
    failed = await add_post(session_factory, broken, 1, PostStatus.FAILED, attempt=3, last_error="ValueError: x")

    html = (await admin.get("/admin")).text

    assert "не запускался" in html  # worker ни разу не отмечался
    assert "Известия" in html and "429 Too Many Requests" in html
    assert "Заглохшая лента" in html and "молчит" in html
    assert "Пост 1" in html and "ValueError: x" in html

    token = await csrf_from(admin, "/admin")
    await admin.post("/admin/failed/retry", data={"csrf_token": token, "post_id": str(failed)})
    assert "Вернул в обработку постов: 1" in (await admin.get("/admin")).text


async def test_summary_needs_admin(logged_in: httpx.AsyncClient) -> None:
    assert (await logged_in.get("/admin")).status_code == 403
