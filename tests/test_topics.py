"""Правила совпадения сюжетов с темами: слова, 👍, 👎, пауза, worker."""

import math
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.config import settings
from sito.models import Topic, TopicExample, TopicMatch, User
from sito.models.post import EMBEDDING_DIM
from sito.repositories.stories import StoryRepository
from sito.repositories.topics import TopicRepository
from sito.services.topics import (
    TopicError,
    TopicMatchingService,
    TopicRules,
    TopicService,
    Vote,
)
from sito.services.users import UserService
from tests.conftest import MakeSource
from tests.test_web_stories import Stories

RULES = TopicRules(min_similarity=0.88, dislike_min_similarity=0.85, backfill=timedelta(days=30))


def direction(degrees: float, base: int = 0, other: int = 1) -> list[float]:
    """Единичный вектор в плоскости осей base/other: сходство двух таких — косинус разницы углов."""
    v = [0.0] * EMBEDDING_DIM
    v[base] = math.cos(math.radians(degrees))
    v[other] = math.sin(math.radians(degrees))
    return v


# Сходство с direction(0): 10° → 0.985, 20° → 0.94, 40° → 0.77, 90° → 0
@pytest.fixture
def stories(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> Stories:
    return Stories(session_factory, make_source)


@pytest.fixture
async def owner(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session, session.begin():
        return await UserService(session).create("olga", "long-password-1")


async def matched(factory: async_sessionmaker[AsyncSession], topic_id: int) -> set[int]:
    async with factory() as session:
        return set(await session.scalars(select(TopicMatch.story_id).where(TopicMatch.topic_id == topic_id)))


async def create(factory: async_sessionmaker[AsyncSession], user: User, query: str | None) -> int:
    async with factory() as session, session.begin():
        return (await TopicService(session, RULES).create(user, query or "тема", query)).id


async def vote(factory: async_sessionmaker[AsyncSession], user: User, topic_id: int, story_id: int, v: Vote) -> None:
    async with factory() as session, session.begin():
        await TopicService(session, RULES).vote(user, topic_id, story_id, v)


async def test_words_of_query_in_any_form(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    """Тема ловит сюжеты, где есть все слова запроса в любой форме; предлоги не нужны."""
    coal = await stories.add([("ТАСС", "В Кузбассе выросла добыча угля", timedelta(hours=1))])
    weather = await stories.add([("ТАСС", "В Кузбассе похолодает", timedelta(hours=1))])
    mines = await stories.add([("РБК", "Шахты увеличили добычу", timedelta(hours=2))])
    gadget = await stories.add([("РБК", "Угол обзора нового смартфона", timedelta(hours=2))])

    topic_id = await create(session_factory, owner, "уголь в кузбассе")

    assert await matched(session_factory, topic_id) == {coal}  # «угля» — это «уголь»; погода без угля — нет
    assert gadget not in await matched(session_factory, topic_id)  # «угол» — другое слово

    or_topic = await create(session_factory, owner, "уголь or шахта -похолодание")
    assert await matched(session_factory, or_topic) == {coal, mines}
    assert weather not in await matched(session_factory, or_topic)


async def test_like_finds_similar_stories(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    example = await stories.add([("ТАСС", "Пример", timedelta(hours=3))], embedding=direction(0))
    close = await stories.add([("ТАСС", "Похожий", timedelta(hours=2))], embedding=direction(20))
    far = await stories.add([("ТАСС", "Далёкий", timedelta(hours=1))], embedding=direction(40))
    topic_id = await create(session_factory, owner, None)

    assert await matched(session_factory, topic_id) == set()  # без запроса и примеров тема пуста

    await vote(session_factory, owner, topic_id, example, Vote.LIKE)

    assert await matched(session_factory, topic_id) == {example, close}
    assert far not in await matched(session_factory, topic_id)


async def test_dislike_removes_story_and_similar(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    good = await stories.add([("ТАСС", "Угольный разрез нарастил добычу", timedelta(hours=3))], embedding=direction(0))
    bad = await stories.add([("ТАСС", "Погода: угольно-чёрные тучи и добыча грибов", timedelta(hours=2))],
                            embedding=direction(90))
    bad_twin = await stories.add([("РБК", "Тучи и добыча грибов", timedelta(hours=1))], embedding=direction(80))
    topic_id = await create(session_factory, owner, "добыча")
    assert await matched(session_factory, topic_id) == {good, bad, bad_twin}

    await vote(session_factory, owner, topic_id, bad, Vote.DISLIKE)

    # bad_twin со словами запроса, но похож на 👎 (0.98) — тоже убран
    assert await matched(session_factory, topic_id) == {good}

    await vote(session_factory, owner, topic_id, bad, Vote.NONE)  # снять отметку — всё возвращается
    assert await matched(session_factory, topic_id) == {good, bad, bad_twin}


async def test_dislike_reaches_wider_than_like(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    """👎 на сюжете, попавшем по случайному совпадению слов, убирает и его соседей послабее (0.85–0.88)."""
    # «Разрез» — и угольный разрез, и разрез на платье
    gadget = await stories.add([("ТАСС", "Платье с разрезом снова в моде", timedelta(hours=3))], embedding=direction(90))
    other_gadget = await stories.add([("ТАСС", "Юбки с разрезом этой осенью", timedelta(hours=2))], embedding=direction(60))
    coal = await stories.add([("ТАСС", "Разрез нарастил добычу", timedelta(hours=1))], embedding=direction(0))
    topic_id = await create(session_factory, owner, "разрез")
    assert await matched(session_factory, topic_id) == {gadget, other_gadget, coal}

    await vote(session_factory, owner, topic_id, gadget, Vote.DISLIKE)

    # сходство other_gadget с 👎 — cos 30° = 0.866: ниже порога 👍, но выше порога 👎
    assert await matched(session_factory, topic_id) == {coal}


async def test_closer_to_like_than_to_dislike_stays(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    liked = await stories.add([("ТАСС", "👍", timedelta(hours=4))], embedding=direction(0))
    disliked = await stories.add([("ТАСС", "👎", timedelta(hours=3))], embedding=direction(24))
    near_like = await stories.add([("ТАСС", "ближе к 👍", timedelta(hours=2))], embedding=direction(8))
    near_dislike = await stories.add([("ТАСС", "ближе к 👎", timedelta(hours=1))], embedding=direction(18))
    topic_id = await create(session_factory, owner, None)

    await vote(session_factory, owner, topic_id, liked, Vote.LIKE)
    await vote(session_factory, owner, topic_id, disliked, Vote.DISLIKE)

    assert await matched(session_factory, topic_id) == {liked, near_like}
    assert near_dislike not in await matched(session_factory, topic_id)


async def test_reason_is_the_closest_example(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    first = await stories.add([("ТАСС", "Первый пример", timedelta(hours=3))], embedding=direction(0))
    second = await stories.add([("ТАСС", "Второй пример", timedelta(hours=3))], embedding=direction(0, base=2, other=3))
    candidate = await stories.add([("ТАСС", "Кандидат", timedelta(hours=1))], embedding=direction(10, base=2, other=3))
    topic_id = await create(session_factory, owner, None)
    await vote(session_factory, owner, topic_id, first, Vote.LIKE)
    await vote(session_factory, owner, topic_id, second, Vote.LIKE)

    async with session_factory() as session:
        [reason] = (await TopicRepository(session).reasons(owner.id, [candidate]))[candidate]
    assert reason.matched and reason.example_title == "Второй пример" and reason.vote is None


async def test_examples_survive_recluster(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    """Сюжеты пересобираются (sito pipeline recluster), а обучение темы остаётся."""
    example = await stories.add([("ТАСС", "Пример", timedelta(hours=2))], embedding=direction(0))
    topic_id = await create(session_factory, owner, None)
    await vote(session_factory, owner, topic_id, example, Vote.LIKE)

    async with session_factory() as session, session.begin():
        await StoryRepository(session).reset_all()
    async with session_factory() as session:
        [kept] = await session.scalars(select(TopicExample).where(TopicExample.topic_id == topic_id))
    assert kept.story_id is None and kept.title == "Пример" and kept.liked

    rebuilt = await stories.add([("ТАСС", "Тот же сюжет заново", timedelta(hours=2))], embedding=direction(5))
    await TopicMatchingService(session_factory, RULES).process_batch()
    assert await matched(session_factory, topic_id) == {rebuilt}


async def test_worker_matches_new_stories_once(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    worker = TopicMatchingService(session_factory, RULES)
    topic_id = await create(session_factory, owner, "уголь")
    assert (await worker.process_batch()).total == 0  # сюжетов нет — работы нет, цикл не крутится

    fresh = await stories.add([("ТАСС", "Цены на уголь выросли", timedelta(minutes=5))])
    batch = await worker.process_batch()

    assert batch.topics == 1 and batch.added == 1
    assert await matched(session_factory, topic_id) == {fresh}
    assert (await worker.process_batch()).total == 0  # уже сопоставлено


async def test_paused_topic_is_skipped_then_catches_up(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    topic_id = await create(session_factory, owner, "уголь")
    async with session_factory() as session, session.begin():
        await TopicService(session, RULES).set_paused(owner, topic_id, True)

    missed = await stories.add([("ТАСС", "Уголь подорожал", timedelta(minutes=5))])
    assert (await TopicMatchingService(session_factory, RULES).process_batch()).total == 0
    assert await matched(session_factory, topic_id) == set()

    async with session_factory() as session, session.begin():
        await TopicService(session, RULES).set_paused(owner, topic_id, False)
    assert await matched(session_factory, topic_id) == {missed}


async def test_old_stories_are_outside_backfill(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    await stories.add([("ТАСС", "Уголь в прошлом году", timedelta(days=settings.topic_backfill_days + 5))])
    recent = await stories.add([("ТАСС", "Уголь сегодня", timedelta(hours=1))])

    topic_id = await create(session_factory, owner, "уголь")

    assert await matched(session_factory, topic_id) == {recent}


async def test_someone_elses_topic(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    story = await stories.add([("ТАСС", "Сюжет", timedelta(hours=1))])
    topic_id = await create(session_factory, owner, "сюжет")
    async with session_factory() as session, session.begin():
        stranger = await UserService(session).create("petr", "long-password-1")

    async with session_factory() as session, session.begin():
        with pytest.raises(TopicError):
            await TopicService(session, RULES).vote(stranger, topic_id, story, Vote.DISLIKE)
    assert await matched(session_factory, topic_id) == {story}


async def test_follow_story(
    session_factory: async_sessionmaker[AsyncSession], stories: Stories, owner: User
) -> None:
    long_title = "Очень длинный заголовок сюжета " * 6
    story = await stories.add([("ТАСС", long_title, timedelta(hours=1))], embedding=direction(0))
    similar = await stories.add([("РБК", "Похожий", timedelta(hours=1))], embedding=direction(15))

    async with session_factory() as session, session.begin():
        topic = await TopicService(session, RULES).follow_story(owner, story)

    async with session_factory() as session:
        saved = await session.get(Topic, topic.id)
    assert saved is not None and saved.query is None
    assert len(saved.name) <= 100 and saved.name.endswith("…")
    assert await matched(session_factory, topic.id) == {story, similar}
