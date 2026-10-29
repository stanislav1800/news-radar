"""Прочитанное, «есть обновления», «Отметить всё прочитанным», «Вы были здесь»."""

import re
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus, User
from sito.repositories.stories import FeedStory, StoryRepository
from sito.services.reading import VISIT_GAP, ReadingService
from sito.web.routes.stories import _divider_before
from tests.conftest import MakeSource
from tests.test_web_stories import NOW, Stories, vec
from tests.web import csrf_from


def card_states(html: str) -> dict[str, str]:
    """Заголовок сюжета → состояние карточки (new / updated / read)."""
    pattern = r'<article class="story-card (\w+)">\s*<a class="story-title" href="/stories/\d+">([^<]+)</a>'
    return {title: state for state, title in re.findall(pattern, html)}


async def test_opened_story_is_read(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    opened = await stories.add([("ТАСС", "Открытый", timedelta(hours=2))])
    await stories.add([("ТАСС", "Неоткрытый", timedelta(hours=1))])

    await logged_in.get(f"/stories/{opened}")

    assert card_states((await logged_in.get("/")).text) == {"Неоткрытый": "new", "Открытый": "read"}


async def test_new_post_after_reading_marks_update(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    story_id = await stories.add([("ТАСС", "Столкнулись поезда", timedelta(hours=2))])
    await logged_in.get(f"/stories/{story_id}")

    # После прочтения в сюжет приклеился пост, опубликованный ещё ДО прочтения:
    # RSS опрашивается раз в несколько минут, посты приходят с опозданием. Это тоже обновление.
    async with session_factory() as session, session.begin():
        source_id = await make_source(name="РБК")
        await session.execute(insert(Post).values(
            source_id=source_id, external_id="later", url="https://news.example/later",
            url_normalized="https://news.example/later", title="Число погибших выросло до 68",
            content_hash="later", published_at=datetime.now(UTC) - timedelta(minutes=30),
            status=PostStatus.CLUSTERED, story_id=story_id, embedding=vec(7),
        ))
        await StoryRepository(session).refresh(story_id)

    html = (await logged_in.get("/")).text

    assert card_states(html) == {"Число погибших выросло до 68": "updated"}
    assert "есть обновления" in html


async def test_mark_all_read(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    await stories.add([("ТАСС", "Первый", timedelta(hours=2))])
    await stories.add([("ТАСС", "Второй", timedelta(hours=1))])

    token = await csrf_from(logged_in, "/")
    response = await logged_in.post("/feed/mark-all-read", data={"csrf_token": token})

    assert response.status_code == 303
    assert set(card_states((await logged_in.get("/")).text).values()) == {"read"}
    assert card_states((await logged_in.get("/?unread=true")).text) == {}


async def test_mark_all_read_requires_csrf(logged_in: httpx.AsyncClient) -> None:
    assert (await logged_in.post("/feed/mark-all-read")).status_code == 403


async def test_unread_filter(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    opened = await stories.add([("ТАСС", "Открытый", timedelta(hours=2))])
    await stories.add([("ТАСС", "Неоткрытый", timedelta(hours=1))])
    await logged_in.get(f"/stories/{opened}")

    assert card_states((await logged_in.get("/?unread=true")).text) == {"Неоткрытый": "new"}


async def test_divider_shows_previous_visit(
    logged_in: httpx.AsyncClient, user: str,
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource,
) -> None:
    stories = Stories(session_factory, make_source)
    await stories.add([("ТАСС", "Старое", timedelta(hours=5))])
    await stories.add([("ТАСС", "Новое", timedelta(hours=1))])
    # Прошлый раз пользователь смотрел ленту 3 часа назад
    async with session_factory() as session, session.begin():
        await session.execute(
            update(User).where(User.username == user)
            .values(last_seen_at=NOW - timedelta(hours=3), previous_visit_at=None)
        )

    html = (await logged_in.get("/")).text

    assert "Вы были здесь 3 ч назад" in html
    assert html.index("Новое") < html.index("Вы были здесь") < html.index("Старое")


def test_visit_boundary_moves_only_after_a_break() -> None:
    user = User(username="u", password_hash="x")
    start = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)

    assert ReadingService.register_visit(user, start) is None  # первый заход: нового «с прошлого раза» нет

    # Через 10 минут — тот же визит: граница не сдвигается
    assert ReadingService.register_visit(user, start + timedelta(minutes=10)) is None

    # После перерыва — новый визит: граница — последний заход прошлого визита
    later = start + timedelta(minutes=10) + VISIT_GAP + timedelta(minutes=1)
    assert ReadingService.register_visit(user, later) == start + timedelta(minutes=10)


def story(story_id: int, hours_ago: float) -> FeedStory:
    at = NOW - timedelta(hours=hours_ago)
    return FeedStory(story_id, "t", None, 1, 1, at, at, None, "new")


def test_divider_position() -> None:
    boundary = NOW - timedelta(hours=3)
    page = [story(1, 1), story(2, 2), story(3, 4), story(4, 5)]

    assert _divider_before(page, boundary, after=None) == 3
    assert _divider_before(page, None, after=None) is None
    # Нового нет: разделитель над самым первым сюжетом был бы бесполезен
    assert _divider_before([story(3, 4), story(4, 5)], boundary, after=None) is None
    # Следующая порция начинается сразу за границей
    assert _divider_before([story(3, 4)], boundary, after=NOW - timedelta(hours=2)) == 3

