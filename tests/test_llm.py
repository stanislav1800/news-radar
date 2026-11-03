"""Пересказ сюжета от LLM: клиент, сборка материалов, кнопка на странице сюжета, лимит."""

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
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
