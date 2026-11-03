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
    app.state.llm = fake_llm([{"error": {"message": "unknown thinking"}},
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
