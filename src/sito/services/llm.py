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
from sito.models import LlmUsage, Story, StorySummary, Topic, TopicMatch, TopicReport
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
    truncated: bool = False  # упёрся в max_tokens


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
            truncated = data["choices"][0].get("finish_reason") == "length"
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("сервис LLM вернул ответ непонятного вида") from exc
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        return LlmAnswer(
            text=text.strip(), model=str(data.get("model") or body["model"]),
            input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0),
            cost=float(cost) if isinstance(cost, int | float) else None, seconds=time.monotonic() - started,
            truncated=truncated,
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


async def check_limit(session: AsyncSession, config: Settings) -> None:
    """Общий лимит токенов за последние сутки: сколько бы ни нажимали, больше него LLM не потратит."""
    used = await usage_since(session, datetime.now(UTC) - timedelta(days=1))
    if used.input_tokens + used.output_tokens >= config.llm_daily_tokens:
        raise LlmError("дневной лимит LLM исчерпан, попробуйте позже")


class StorySummaryService:
    FEATURE = "story_summary"

    def __init__(self, session: AsyncSession, client: LlmClient, config: Settings) -> None:
        self.session = session
        self.client = client
        self.config = config

    async def get(self, story_id: int) -> StorySummary | None:
        return await self.session.get(StorySummary, story_id)


    async def summarize(
        self, story_id: int, *, user_id: int | None, timezone: str = "UTC", model: str | None = None,
        save: bool = True,
    ) -> tuple[StorySummary, LlmAnswer]:
        story = await self.session.get(Story, story_id)
        if story is None:
            raise LlmError("сюжет не найден")
        await check_limit(self.session, self.config)
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


PERIODS = {1: "день", 7: "неделю", 30: "месяц"}

_TOPIC_INTRO = """Ты — аналитик-редактор новостей. Тебе дают сюжеты по теме «{name}» за последние {period}. \
У каждого сюжета номер в квадратных скобках, дата, число изданий (чем больше — тем заметнее событие) \
и содержание: пересказ или текст статей."""

_TOPIC_RULES = """Пиши только то, что есть в материалах, ничего не додумывай; если издания расходятся — укажи это. \
После каждого факта ставь номер сюжета-источника в квадратных скобках: [3] или [3, 7]. \
Формат — markdown: заголовки «## », списки «- », **жирный** для ключевого; без таблиц и без общего заголовка \
в начале."""

TOPIC_SUMMARY_SYSTEM = _TOPIC_INTRO + """

Напиши обзор темы за период:
- начни с раздела «## Главное»: 3–5 коротких пунктов, по одному предложению — самое важное за период;
- затем разделы по направлениям или подтемам: что произошло, подробности, цифры, заявления, как развивались события;
- в конце, если есть, «## Что дальше»: объявленные планы, сроки, ожидаемые решения.
Объём — по материалу: если сюжетов мало, обзор короткий, без лишних разделов; если много — подробный. \
Важное описывай подробнее, мелочи — одной строкой или пропусти. Весь обзор — не длиннее {words} слов.

""" + _TOPIC_RULES

TOPIC_QUESTION_SYSTEM = _TOPIC_INTRO + """

Ответь на вопрос пользователя только по этим материалам. Если ответа в них нет или он неполный — прямо скажи \
об этом. Объём ответа — по вопросу, но не длиннее {words} слов.

""" + _TOPIC_RULES

USER_PROMPT = """

Пожелания пользователя к ответу (выполняй, если они не требуют выдумывать факты или нарушать правила выше):
{prompt}"""


def topic_prompt(
    stories: list[tuple[int, str, int, datetime, str | None]], materials: dict[int, list[MaterialPost]],
    budget_chars: int, tz: ZoneInfo,
) -> str:
    """Сюжеты темы по времени. stories: (id, заголовок, изданий, начало, пересказ LLM или None).

    Место делится поровну; у сюжета с пересказом берётся пересказ, иначе самый длинный текст и список изданий.
    """
    share = min(3000, max(400, budget_chars // max(1, len(stories))))
    parts = []
    for n, (story_id, title, sources, started_at, summary) in enumerate(stories, 1):
        at = started_at.astimezone(tz)
        posts = materials.get(story_id, [])
        outlets = list(dict.fromkeys(p.source_name for p in posts))
        head = f"[{n}] {at.day} {MONTHS[at.month - 1]}, изданий: {sources}"
        if summary:
            body = _cut(summary, share)
        else:
            best = max(posts, key=lambda p: len(p.body), default=None)
            body = _cut(best.body, share) if best and best.body else ""
            if outlets:
                body = f"{body}\n(пишут: {', '.join(outlets[:8])})".strip()
        parts.append(f"{head}\n{title}\n{body}".rstrip())
    return "\n\n".join(parts)


class TopicReportService:
    def __init__(self, session: AsyncSession, client: LlmClient, config: Settings) -> None:
        self.session = session
        self.client = client
        self.config = config

    def _since(self, days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=days)

    async def _stories(self, topic: Topic, days: int) -> list[tuple[int, str, int, datetime, str | None]]:
        """Самые заметные сюжеты темы за период, по времени."""
        rows = list(await self.session.execute(
            select(Story.id, func.coalesce(StorySummary.title, Story.title), Story.sources_count, Story.started_at,
                   StorySummary.summary)
            .join(TopicMatch, TopicMatch.story_id == Story.id)
            .outerjoin(StorySummary, StorySummary.story_id == Story.id)
            .where(TopicMatch.topic_id == topic.id, Story.last_post_at >= self._since(days))
            .order_by(Story.sources_count.desc(), Story.last_post_at.desc())
            .limit(self.config.llm_topic_stories)
        ))
        return sorted(((r[0], r[1], r[2], r[3], r[4]) for r in rows), key=lambda r: r[3])

    async def new_since(self, report: TopicReport) -> int:
        """Сколько сюжетов темы появилось или пополнилось после отчёта."""
        return int(await self.session.scalar(
            select(func.count()).select_from(TopicMatch).join(Story, Story.id == TopicMatch.story_id)
            .where(TopicMatch.topic_id == report.topic_id, Story.last_post_at > report.created_at)
        ) or 0)

    async def latest(self, topic_id: int, kind: str | None = None, days: int | None = None) -> TopicReport | None:
        query = select(TopicReport).where(TopicReport.topic_id == topic_id)
        if kind:
            query = query.where(TopicReport.kind == kind)
        if days:
            query = query.where(TopicReport.days == days)
        return await self.session.scalar(query.order_by(TopicReport.id.desc()).limit(1))

    async def make(
        self, topic: Topic, *, days: int, user_id: int, timezone: str = "UTC", question: str | None = None,
        force: bool = False,
    ) -> TopicReport:
        """Сводка за период (question=None) или ответ на вопрос. Сводку без новых сюжетов не переделывает."""
        if days not in PERIODS:
            raise LlmError("неизвестный период")
        kind = "question" if question else "summary"
        if kind == "summary" and not force:
            last = await self.latest(topic.id, kind, days)
            if last is not None and not await self.new_since(last):
                return last
        stories = await self._stories(topic, days)
        if not stories:
            raise LlmError(f"в теме нет сюжетов за {PERIODS[days]}")
        await check_limit(self.session, self.config)
        materials = await StoryRepository(self.session).materials(
            [s[0] for s in stories if not s[4]], body_chars=3000
        )
        system = (TOPIC_QUESTION_SYSTEM if question else TOPIC_SUMMARY_SYSTEM).format(
            name=topic.name, period={1: "сутки", 7: "7 дней", 30: "30 дней"}[days],
            words={1: 500, 7: 1000, 30: 1500}[days],
        )
        if topic.llm_prompt and topic.llm_prompt.strip():
            system += USER_PROMPT.format(prompt=topic.llm_prompt.strip())
        prompt = "Сюжеты:\n\n" + topic_prompt(stories, materials, self.config.llm_topic_chars, ZoneInfo(timezone))
        if question:
            prompt += f"\n\nВопрос: {question}"
        answer = await self.client.complete(system, prompt, max_tokens=self.config.llm_report_max_tokens)
        self.session.add(LlmUsage(
            feature=f"topic_{kind}", model=answer.model, input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens, cost=answer.cost, user_id=user_id,
        ))
        if not answer.text:
            await self.session.flush()
            raise LlmError("модель вернула пустой ответ, попробуйте ещё раз")
        text = answer.text + ("\n\n(Ответ обрезан: модель упёрлась в лимит длины.)" if answer.truncated else "")
        report = TopicReport(
            topic_id=topic.id, kind=kind, days=days, question=question, text=text,
            story_ids=[s[0] for s in stories], model=answer.model, input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens, cost=answer.cost,
        )
        self.session.add(report)
        await self.session.flush()
        return report
