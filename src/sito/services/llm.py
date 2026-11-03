"""LLM: пересказ сюжета по кнопке.

Клиент говорит с любым сервисом в формате OpenAI Chat Completions (routerai.ru, OpenRouter, DeepSeek,
Ollama). Каждый запрос пишется в llm_usage: по нему дневной лимит и счётчик в админке.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sito.config import Settings
from sito.models import LlmUsage, Story, StorySummary
from sito.repositories.stories import MaterialPost, StoryRepository


class LlmError(Exception):
    """Сервис LLM недоступен, ответил ошибкой или исчерпан лимит: текст можно показать пользователю."""


@dataclass(frozen=True, slots=True)
class LlmAnswer:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float | None
    seconds: float


class LlmClient:
    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: float = 90, no_reasoning: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.no_reasoning = no_reasoning
        self._reasoning_rejected: set[str] = set()
        self.transport = transport  # тесты подменяют, чтобы не ходить в сеть

    @classmethod
    def from_settings(cls, config: Settings) -> "LlmClient | None":
        """None, если LLM не настроена."""
        key = config.llm_api_key.get_secret_value() if config.llm_api_key else ""
        if not (config.llm_base_url and key and config.llm_model):
            return None
        return cls(config.llm_base_url, key, config.llm_model, config.llm_timeout, config.llm_no_reasoning)

    async def _post(self, body: dict[str, object]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as http:
                response = await http.post(
                    f"{self.base_url}/chat/completions", json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise LlmError(f"сервис LLM недоступен: {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise LlmError(f"сервис LLM ответил {response.status_code}: {response.text[:200]}")
        if response.status_code != 200 and "error" not in data:
            data["error"] = f"HTTP {response.status_code}"
        return data

    async def complete(self, system: str, prompt: str, *, max_tokens: int, model: str | None = None) -> LlmAnswer:
        started = time.monotonic()
        name = model or self.model
        body: dict[str, object] = {
            "model": name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if self.no_reasoning and name not in self._reasoning_rejected:
            body["thinking"] = {"type": "disabled"}
        data = await self._post(body)
        if "error" in data and "thinking" in body:
            # Сервис или модель не знает этого параметра: повторить без него
            self._reasoning_rejected.add(name)
            del body["thinking"]
            data = await self._post(body)
        if "error" in data:
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise LlmError(f"сервис LLM ответил ошибкой: {str(message)[:200]}")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("сервис LLM вернул ответ непонятного вида") from exc
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        return LlmAnswer(
            text=text.strip(), model=str(data.get("model") or body["model"]),
            input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0),
            cost=float(cost) if isinstance(cost, int | float) else None, seconds=time.monotonic() - started,
        )


SUMMARY_SYSTEM = """Ты — редактор новостного агрегатора. Тебе дают материалы разных изданий об одном событии \
в порядке публикации. Ответь строго в таком виде:
первая строка — заголовок сюжета: нейтральный, по сути события, до 100 знаков, без кавычек вокруг и без точки;
пустая строка;
пересказ. Его объём зависит от материала. Если фактов мало — 2–4 предложения. Если материал богатый \
(подробности, цифры, заявления, реакции, развитие событий) — развёрнуто, 2–5 абзацев, разделённых пустой \
строкой: что произошло; подробности и цифры; заявления и реакции сторон; где издания расходятся в фактах \
или оценках (кто что пишет); что известно сейчас, если событие развивалось. Пропускай пункты, о которых \
в материале ничего нет.

Пиши только то, что есть в материалах, ничего не додумывай. Не повторяй одно и то же разными словами. \
Без вступлений вроде «В материалах сообщается», без markdown и списков. Реклама, призывы подписаться \
и посторонние новости из материалов — не часть события, пропускай их."""


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[: cut.rfind(" ")] if " " in cut else cut + "…"


def story_prompt(posts: list[MaterialPost], budget_chars: int, tz: ZoneInfo) -> str:
    """Материалы сюжета в пределах budget_chars знаков.

    Каждое издание получает поровну места, у издания приоритет у самого длинного поста (обычно полная статья).
    Посты, на которые места не хватило, идут одним заголовком: модель видит, что о событии писали и они.
    """
    unique: list[MaterialPost] = []
    seen: set[tuple[str, str]] = set()
    for post in posts:  # репосты и одинаковые посты одного текста — один раз
        key = (post.title.strip().lower(), post.body[:200])
        if key not in seen:
            seen.add(key)
            unique.append(post)
    by_outlet: dict[str, list[MaterialPost]] = {}
    for post in unique:
        by_outlet.setdefault(post.outlet, []).append(post)
    # Очередь: сначала лучший пост каждого издания, потом остальные
    best = [max(group, key=lambda p: len(p.body)) for group in by_outlet.values()]
    rest = [p for p in unique if p not in best]
    budget = budget_chars
    texts: dict[int, str] = {}
    for i, post in enumerate(best + rest):
        left = len(best) + len(rest) - i
        share = max(1500, budget // max(1, left))
        if budget < 300:
            break
        body = _cut(post.body, min(share, budget))
        texts[id(post)] = body
        budget -= len(body) + len(post.title) + 50
    parts: list[str] = []
    for n, post in enumerate(unique, 1):
        at = post.event_at.astimezone(tz)
        head = f"[{n}] {post.source_name}, {at.day} {MONTHS[at.month - 1]} {at:%H:%M}"
        text = texts.get(id(post))
        title = post.title or "(без заголовка)"
        parts.append(f"{head}\n{title}\n{text}" if text else f"{head}\n{title}\n(текст не приводится)")
    return "\n\n".join(parts)


def parse_summary(text: str) -> tuple[str, str]:
    """Заголовок и пересказ из ответа модели (первая строка — заголовок)."""
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    lines = [re.sub(r"^\**(заголовок|пересказ)\s*:\s*", "", line, flags=re.IGNORECASE).strip("*# ").strip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return "", ""
    title = lines[0].rstrip(".").strip("«»\"' ").rstrip(".")
    summary = "\n".join(line for line in lines[1:] if line).strip()  # абзацы — по строке
    return title, summary


@dataclass(frozen=True, slots=True)
class LlmUsageStats:
    requests: int
    input_tokens: int
    output_tokens: int
    cost: float | None  # сумма цен, которые сообщил сервис (у routerai.ru — в рублях)


async def usage_since(session: AsyncSession, since: datetime) -> LlmUsageStats:
    row = (await session.execute(
        select(func.count(), func.coalesce(func.sum(LlmUsage.input_tokens), 0),
               func.coalesce(func.sum(LlmUsage.output_tokens), 0), func.sum(LlmUsage.cost))
        .where(LlmUsage.created_at >= since)
    )).one()
    return LlmUsageStats(int(row[0]), int(row[1]), int(row[2]), float(row[3]) if row[3] is not None else None)


class StorySummaryService:
    FEATURE = "story_summary"

    def __init__(self, session: AsyncSession, client: LlmClient, config: Settings) -> None:
        self.session = session
        self.client = client
        self.config = config

    async def get(self, story_id: int) -> StorySummary | None:
        return await self.session.get(StorySummary, story_id)

    async def used_today(self) -> int:
        since = datetime.now(UTC) - timedelta(days=1)
        used = await self.session.scalar(
            select(func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0))
            .where(LlmUsage.created_at >= since)
        )
        return int(used or 0)

    async def summarize(
        self, story_id: int, *, user_id: int | None, timezone: str = "UTC", model: str | None = None,
        save: bool = True,
    ) -> tuple[StorySummary, LlmAnswer]:
        story = await self.session.get(Story, story_id)
        if story is None:
            raise LlmError("сюжет не найден")
        if await self.used_today() >= self.config.llm_daily_tokens:
            raise LlmError("дневной лимит LLM исчерпан, попробуйте позже")
        posts = await StoryRepository(self.session).material(story_id)
        prompt = story_prompt(posts, self.config.llm_story_chars, ZoneInfo(timezone))
        answer = await self.client.complete(
            SUMMARY_SYSTEM, prompt, max_tokens=self.config.llm_summary_max_tokens, model=model
        )
        self.session.add(LlmUsage(
            feature=self.FEATURE, model=answer.model, input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens, cost=answer.cost, user_id=user_id,
        ))
        title, summary = parse_summary(answer.text)
        if not summary:
            await self.session.flush()
            raise LlmError("модель вернула пустой пересказ, попробуйте ещё раз")
        values = {"title": title, "summary": summary, "model": answer.model,
                  "posts_count": story.posts_count, "sources_count": story.sources_count}
        if not save:
            return StorySummary(story_id=story_id, **values), answer
        current = await self.get(story_id)
        if current is None:
            current = StorySummary(story_id=story_id, **values)
            self.session.add(current)
        else:
            for name, value in values.items():
                setattr(current, name, value)
        await self.session.flush()
        return current, answer
