"""Пересказ сюжета от LLM: клиент, сборка материалов, кнопка на странице сюжета, лимит."""

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import LlmUsage, StorySummary
from sito.repositories.stories import MaterialPost
from sito.services.llm import LlmClient, parse_summary, story_prompt
from tests.conftest import MakeSource
from tests.test_web_stories import NOW, Stories
from tests.web import csrf_from


@pytest.fixture
def stories(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> Stories:
    return Stories(session_factory, make_source)


def fake_llm(answers: list[dict[str, object]], seen: list[dict[str, object]]) -> LlmClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=answers.pop(0))

    return LlmClient("https://llm.example/v1", "key", "test/model", no_reasoning=True,
                     transport=httpx.MockTransport(handle))


def answer(text: str) -> dict[str, object]:
    return {"model": "test/model", "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.12}}


def test_parse_summary() -> None:
    assert parse_summary("**Заголовок:** «ЦБ снизил ставку».\n\nПервое.\nВторое.") == (
        "ЦБ снизил ставку", "Первое.\nВторое.")
    assert parse_summary("") == ("", "")


def test_story_prompt_fits_budget_and_lists_every_outlet() -> None:
    long = "Слово " * 3000
    posts = [MaterialPost("ТАСС", "tass", "Статья", NOW, long),
             MaterialPost("ТАСС", "tass", "Статья", NOW, long),  # репост — один раз
             MaterialPost("РБК", "rbc", "Заметка", NOW + timedelta(minutes=5), "Коротко о главном.")]
    prompt = story_prompt(posts, 5000, ZoneInfo("Europe/Moscow"))
    assert len(prompt) < 5500
    assert prompt.count("ТАСС,") == 1 and "[2] РБК" in prompt and "Коротко о главном." in prompt


async def test_summary_button_saves_and_counts_usage(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    story_id = await stories.add([("ТАСС", "Рекорд добычи угля", timedelta(hours=2)),
                                  ("РБК", "Кузбасс добыл больше угля", timedelta(hours=1))])
    page = await logged_in.get(f"/stories/{story_id}")
    assert "Кратко пересказать" not in page.text  # LLM не настроена — кнопки нет

    seen: list[dict[str, object]] = []
    # Первый ответ — сервис не знает параметра thinking: клиент повторит без него
    app.state.llm = fake_llm([{"error": {"message": "unknown parameter: thinking"}},
                              answer("Кузбасс добыл рекорд угля\n\nШахтёры добыли рекорд. Пишут ТАСС и РБК.")], seen)
    page = await logged_in.get(f"/stories/{story_id}")
    assert "Кратко пересказать сюжет" in page.text
    token = await csrf_from(logged_in, f"/stories/{story_id}")
    response = await logged_in.post(f"/stories/{story_id}/summary", data={"csrf_token": token})
    assert response.status_code == 200 and "Кузбасс добыл рекорд угля" in response.text
    assert "thinking" in seen[0] and "thinking" not in seen[1]
    assert "Рекорд добычи угля" in str(seen[1]["messages"])

    async with session_factory() as session:
        summary = await session.get(StorySummary, story_id)
        usage = list(await session.scalars(select(LlmUsage)))
    assert summary is not None and summary.posts_count == 2
    assert [(u.input_tokens, u.output_tokens, u.cost) for u in usage] == [(1000, 100, 0.12)]
    assert "Кузбасс добыл рекорд угля" in (await logged_in.get(f"/stories/{story_id}")).text

    # «Пересказать заново»: пересказ обновляется, и ответ показывается сразу (время обновления ставит база)
    app.state.llm = fake_llm([answer("Рекорд угля в Кузбассе\n\nОбновлённый пересказ.")], seen)
    response = await logged_in.post(f"/stories/{story_id}/summary", data={"csrf_token": token})
    assert response.status_code == 200 and "Обновлённый пересказ." in response.text


async def test_daily_limit(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    story_id = await stories.add([("ТАСС", "Рекорд добычи угля", timedelta(hours=1))])
    async with session_factory() as session, session.begin():
        session.add(LlmUsage(feature="story_summary", model="m", input_tokens=10**9, output_tokens=0))
    seen: list[dict[str, object]] = []
    app.state.llm = fake_llm([], seen)
    token = await csrf_from(logged_in, f"/stories/{story_id}")
    response = await logged_in.post(f"/stories/{story_id}/summary", data={"csrf_token": token})
    assert "лимит" in response.text and seen == []


def test_report_html_escapes_and_links_stories() -> None:
    from sito.web.formatting import report_html

    html = report_html("## Главное\n**Рекорд** угля [1, 2]. <script>\n- пункт [3]\n- [9]", [10, 20, 30])
    assert "<h3>Главное</h3>" in html and "<strong>Рекорд</strong>" in html and "&lt;script&gt;" in html
    assert '<a href="/stories/10">1</a>, <a href="/stories/20">2</a>' in html
    assert "<ul><li>пункт" in html and "9" not in html.split("</ul>")[0].split("пункт")[1]


async def test_topic_summary_question_history_and_prompt(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sito.models import Topic, TopicMatch, TopicReport, User

    story_id = await stories.add([("ТАСС", "Рекорд добычи угля", timedelta(hours=2)),
                                  ("РБК", "Кузбасс добыл больше угля", timedelta(hours=1))])
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User))
        assert user is not None
        topic = Topic(user_id=user.id, name="Уголь", query="уголь", rss_token="t", llm_prompt="Сначала цифры")
        session.add(topic)
        await session.flush()
        session.add(TopicMatch(topic_id=topic.id, story_id=story_id, by_words=True))
        topic_id = topic.id

    seen: list[dict[str, object]] = []
    app.state.llm = fake_llm([answer("## Главное\nДобыли рекорд [1]."), answer("Сроков нет [1].")], seen)
    page = await logged_in.get(f"/?topic={topic_id}")
    assert "Сводка за" in page.text and "Спросить про эту тему" in page.text
    token = await csrf_from(logged_in, f"/?topic={topic_id}")
    url = f"/topics/{topic_id}/reports"

    response = await logged_in.post(url, data={"csrf_token": token, "days": "7"})
    assert f'<a href="/stories/{story_id}">1</a>' in response.text
    system = str(seen[0]["messages"])
    assert "«Уголь»" in system and "Сначала цифры" in system and "Кузбасс добыл больше угля" in system

    # Новых сюжетов нет — та же сводка без запроса к LLM
    await logged_in.post(url, data={"csrf_token": token, "days": "7"})
    assert len(seen) == 1

    response = await logged_in.post(url, data={"csrf_token": token, "days": "1", "question": "Какие сроки?"})
    assert "Сроков нет" in response.text and "Вопрос: Какие сроки?" in str(seen[1]["messages"])

    history = await logged_in.get(url)
    assert "Сводка за неделю" in history.text and "Вопрос: Какие сроки?" in history.text
    async with session_factory() as session:
        assert len(list(await session.scalars(select(TopicReport)))) == 2
        assert len(list(await session.scalars(select(LlmUsage)))) == 2


def test_digest_slots() -> None:
    from datetime import UTC, datetime

    from sito.models import Topic
    from sito.services.llm import digest_slot, next_digest

    msk = ZoneInfo("Europe/Moscow")
    wednesday_10 = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)  # 10:00 МСК, среда
    assert digest_slot("daily", "08:00", 0, wednesday_10, msk) == datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
    assert digest_slot("daily", "12:00", 0, wednesday_10, msk) == datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert digest_slot("weekly", "08:00", 0, wednesday_10, msk) == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    saturday = datetime(2026, 11, 7, 7, 0, tzinfo=UTC)
    assert digest_slot("weekdays", "08:00", 0, saturday, msk) == datetime(2026, 11, 6, 5, 0, tzinfo=UTC)
    topic = Topic(digest_schedule="weekdays", digest_time="08:00", digest_weekday=0)
    assert next_digest(topic, saturday, msk) == datetime(2026, 11, 9, 5, 0, tzinfo=UTC)


async def test_digest_by_schedule_page_badge_and_rss(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    from sito.models import Topic, TopicMatch, TopicReport, User
    from sito.services.llm import DigestService

    story_id = await stories.add([("ТАСС", "Рекорд добычи угля", timedelta(hours=2))])
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User))
        assert user is not None
        topic = Topic(user_id=user.id, name="Уголь", query="уголь", rss_token="tok")
        session.add(topic)
        await session.flush()
        session.add(TopicMatch(topic_id=topic.id, story_id=story_id, by_words=True))
        topic_id = topic.id

    token = await csrf_from(logged_in, f"/topics/{topic_id}")
    await logged_in.post(f"/topics/{topic_id}/digest", data={
        "csrf_token": token, "schedule": "daily", "at": "08:00", "kind": "short", "min_stories": "2",
        "prompt": "Только цифры"})
    seen: list[dict[str, object]] = []
    client = fake_llm([answer("- Рекорд угля [1].")], seen)
    service = DigestService(session_factory, client, app.state.settings)
    tomorrow = datetime.now(UTC) + timedelta(days=1, hours=1)

    # Сразу после сохранения — не срабатывает; завтра — мало новых сюжетов (1 < 2): пропуск без запроса
    assert await service.run_due() == 0
    assert await service.run_due(tomorrow) == 0 and seen == []

    async with session_factory() as session, session.begin():
        topic = await session.get(Topic, topic_id)
        assert topic is not None
        topic.digest_min_stories, topic.digest_last_at = 1, None
    assert await service.run_due(tomorrow) == 1
    assert "Только цифры" in str(seen[0]["messages"]) and "дайджест" in str(seen[0]["messages"])
    assert await service.run_due(tomorrow) == 0  # этот слот уже отработан

    assert ">1</span>" in (await logged_in.get("/digests/badge")).text
    page = await logged_in.get("/digests")
    assert "Рекорд угля" in page.text and f'href="/stories/{story_id}"' in page.text
    assert "</span>" not in (await logged_in.get("/digests/badge")).text  # прочитан
    rss = await logged_in.get("/rss/tok/digests")
    assert "Рекорд угля" in rss.text and f"/stories/{story_id}" in rss.text
    async with session_factory() as session:
        assert [r.kind for r in await session.scalars(select(TopicReport))] == ["digest"]


def test_parse_draft() -> None:
    from sito.services.llm import LlmError, parse_draft

    text = '```json\n{"name": "Уголь", "query": "уголь OR шахта", "description": "Добыча", "explanation": "Синонимы"}\n```'
    assert parse_draft(text)["query"] == "уголь OR шахта"
    with pytest.raises(LlmError):
        parse_draft("не знаю")


async def test_topic_from_description(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sito.models import Topic

    await stories.add([("ТАСС", "Рекорд добычи угля в Кузбассе", timedelta(hours=2))])

    def draft(query: str) -> dict[str, object]:
        return answer(json.dumps({"name": "Уголь", "query": query, "description": "Добыча угля, без цен",
                                  "explanation": "Уголь и синонимы"}, ensure_ascii=False))

    seen: list[dict[str, object]] = []
    # Первый запрос ничего не находит — модель просят расширить
    app.state.llm = fake_llm([draft("кобальт"), draft("уголь OR угольный")], seen)
    assert "Составить тему" in (await logged_in.get("/topics/new?d=уголь")).text
    token = await csrf_from(logged_in, "/topics/new")
    response = await logged_in.post("/topics/draft", data={"csrf_token": token, "description": "добыча угля"})
    assert 'value="уголь OR угольный"' in response.text and "Рекорд добычи угля" in response.text
    assert "модель его поправила" in response.text and "не нашёл ни одной" in str(seen[1]["messages"])

    checked = await logged_in.post("/topics/draft/check", data={"csrf_token": token, "query": "уголь -рекорд"})
    assert "ничего не нашёл" in checked.text and len(seen) == 2

    created = await logged_in.post("/topics", data={
        "csrf_token": token, "name": "Уголь", "query": "уголь OR угольный", "topic_description": "Добыча угля"})
    assert created.status_code == 303
    async with session_factory() as session:
        topic = await session.scalar(select(Topic))
    assert topic is not None and topic.description == "Добыча угля"


async def test_topic_check_removes_and_restore_returns(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sito.models import Post, Topic, TopicCheck, TopicMatch, User
    from sito.services.llm import TopicCheckService
    from sito.services.topics import TopicRules, TopicService

    good = await stories.add([("ТАСС", "Рекорд добычи угля", timedelta(hours=2))])  # явно: слово в заголовке
    # Спорный: слово мелькнуло в тексте одного поста из трёх
    bad = await stories.add([("РБК", "Новый герб города", timedelta(hours=1)), ("ТАСС", "Герб утвердили", timedelta(hours=1)),
                             ("РИА", "Городу дали герб", timedelta(hours=1))])
    async with session_factory() as session, session.begin():
        first = await session.scalar(select(Post).where(Post.story_id == bad).order_by(Post.id).limit(1))
        assert first is not None
        first.text_lemmas = f"{first.text_lemmas} на гербе добыча уголь"
    async with session_factory() as session, session.begin():
        user = await session.scalar(select(User))
        assert user is not None
        topic = Topic(user_id=user.id, name="Уголь", query="добыча угля", rss_token="c", llm_check=True,
                      description="Добыча и экспорт угля")
        session.add(topic)
        await session.flush()
        await TopicService(session, TopicRules.from_settings(app.state.settings)).rematch(topic)
        topic_id = topic.id

    async def matched() -> set[int]:
        async with session_factory() as session:
            return set(await session.scalars(select(TopicMatch.story_id).where(TopicMatch.topic_id == topic_id)))

    assert await matched() == {good, bad}
    seen: list[dict[str, object]] = []
    verdicts = [{"n": 1, "fits": False, "why": "про герб"}]
    client = fake_llm([answer(json.dumps(verdicts, ensure_ascii=False))], seen)
    assert await TopicCheckService(session_factory, client, app.state.settings).run_once() == (2, 1)
    # Модели — только спорный сюжет; явный отмечен без неё
    messages = str(seen[0]["messages"])
    assert "Добыча и экспорт угля" in messages and "герб" in messages and "Рекорд" not in messages
    assert await matched() == {good}

    # Пересчёт темы не возвращает отклонённое
    async with session_factory() as session, session.begin():
        topic = await session.get(Topic, topic_id)
        assert topic is not None
        await TopicService(session, TopicRules.from_settings(app.state.settings)).rematch(topic)
    assert await matched() == {good}

    app.state.llm = client
    page = await logged_in.get(f"/topics/{topic_id}")
    assert f'href="/stories/{bad}"' in page.text and "про герб" in page.text
    token = await csrf_from(logged_in, f"/topics/{topic_id}")
    await logged_in.post(f"/topics/{topic_id}/checks/{bad}/restore", data={"csrf_token": token})
    assert await matched() == {good, bad}
    async with session_factory() as session:
        check = await session.get(TopicCheck, (topic_id, bad))
    assert check is not None and check.fits


def test_parse_verdicts_keeps_answers_before_cut() -> None:
    from sito.services.llm import parse_verdicts

    text = '```json\n[\n {"n": 1, "fits": true, "why": "да"},\n {"n": 2, "fits": false, "why": "нет"},\n {"n": 3, "fi'
    assert parse_verdicts(text) == {1: (True, "да"), 2: (False, "нет")}


async def test_user_daily_limit(session_factory: async_sessionmaker[AsyncSession], user: str) -> None:
    from sito.config import settings
    from sito.models import User
    from sito.services.llm import LlmError, check_limit

    config = settings.model_copy(update={"llm_daily_tokens": 10**9, "llm_user_daily_tokens": 1000})
    async with session_factory() as session, session.begin():
        me = await session.scalar(select(User))
        assert me is not None
        session.add(LlmUsage(feature="story_summary", model="m", input_tokens=900, output_tokens=200, user_id=me.id))
        await session.flush()
        await check_limit(session, config)  # общий лимит не исчерпан
        with pytest.raises(LlmError, match="ваш дневной лимит"):
            await check_limit(session, config, me.id)


async def test_story_merge_by_llm(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource,
) -> None:
    from sqlalchemy import update

    from sito.models import Post, Story, StoryMergeCheck
    from sito.services.clustering import ClusteringService
    from sito.services.llm import StoryMergeService
    from tests.test_clustering import T0, Posts, vec

    a, b, c = await make_source(), await make_source(), await make_source()
    posts = Posts(session_factory)
    # Одно событие, второе издание написало через 20 часов: склейка (до 6 часов) их не соединила
    first = await posts.add(a, vec(), T0, title="В Белове спасли 10 человек из горящего дома")
    late = await posts.add(b, vec(0.97), T0 + timedelta(hours=20), title="В Белове МЧС спасли 10 человек")
    # Повторившийся похожий случай: модель скажет «разные»
    await posts.add(a, vec(0.95, axis=6), T0 + timedelta(hours=100), title="Во Внукове ввели ограничения")
    again = await posts.add(c, vec(0.96, axis=7), T0 + timedelta(hours=108), title="Во Внукове снова ограничения")
    await ClusteringService(session_factory).process_batch()
    async with session_factory() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(Story)) == 4
        # Сюжеты созданы «давно»: свежие (меньше 2 минут) не проверяются, склейка могла ещё не закончить
        await session.execute(update(Story).values(created_at=func.now() - timedelta(minutes=10)))

    seen: list[dict[str, object]] = []
    verdicts = [{"n": 1, "same": True, "why": "одно спасение"}, {"n": 2, "same": False, "why": "новый случай"}]
    client = fake_llm([answer(json.dumps(verdicts, ensure_ascii=False))], seen)
    assert await StoryMergeService(session_factory, client, app.state.settings).run_once() == (2, 1)
    assert "Во Внукове снова ограничения" in str(seen[0]["messages"])

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Story)) == 3
        story_ids = {p.id: p.story_id for p in await session.scalars(select(Post))}
        merged = await session.get(Story, story_ids[first])
        check = await session.get(StoryMergeCheck, story_ids[again])
    assert story_ids[first] == story_ids[late] and merged is not None and merged.posts_count == 2
    assert check is not None and check.same is False and check.reason == "новый случай"

    # Проверенное повторно модели не уходит
    assert await StoryMergeService(session_factory, client, app.state.settings).run_once() == (0, 0)


async def test_merge_keeps_reads_topics_and_report_links(
    stories: Stories, session_factory: async_sessionmaker[AsyncSession], user: str,
) -> None:
    from sito.models import (
        StoryRead,
        Topic,
        TopicExample,
        TopicMatch,
        TopicReport,
        User,
    )
    from sito.repositories.stories import StoryRepository

    keep = await stories.add([("ТАСС", "Спасли 10 человек", timedelta(hours=20))])
    gone = await stories.add([("РБК", "МЧС спасли 10 человек", timedelta(hours=1))])
    async with session_factory() as session, session.begin():
        me = await session.scalar(select(User))
        assert me is not None
        topic = Topic(user_id=me.id, name="МЧС", rss_token="m")
        session.add(topic)
        await session.flush()
        session.add_all([
            StoryRead(user_id=me.id, story_id=gone),
            TopicMatch(topic_id=topic.id, story_id=gone, by_words=True),
            TopicExample(topic_id=topic.id, story_id=gone, liked=True, title="МЧС", embedding=[0.0] * 767 + [1.0]),
            TopicReport(topic_id=topic.id, kind="summary", days=1, text="[1]", story_ids=[gone], model="m",
                        input_tokens=1, output_tokens=1),
        ])
        await session.flush()
        await StoryRepository(session).merge(gone, keep)

    async with session_factory() as session:
        assert await session.get(StoryRead, (me.id, keep)) is not None
        assert await session.get(TopicMatch, (topic.id, keep)) is not None
        assert (await session.scalar(select(TopicExample))).story_id == keep  # type: ignore[union-attr]
        assert (await session.scalar(select(TopicReport))).story_ids == [keep]  # type: ignore[union-attr]
