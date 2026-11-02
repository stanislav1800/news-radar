"""Темы пользователя: сохранённый поиск, который учится на 👍/👎 (шаг 7 пайплайна)."""

import logging
import secrets
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.config import Settings
from sito.models import Topic, TopicExample, User
from sito.repositories.stories import StoryRepository
from sito.repositories.topics import MatchResult, TopicRepository

logger = logging.getLogger(__name__)

NAME_MAX = 100
QUERY_MAX = 300
# Worker пересматривает сюжеты, изменённые чуть раньше отметки: транзакция склейки может
# записать updated_at раньше, а закоммититься позже, чем worker прочитал отметку
MATCH_OVERLAP = timedelta(minutes=5)
NEVER = datetime(1970, 1, 1, tzinfo=UTC)


class TopicError(Exception):
    """Действие с темой не выполнено; текст — для пользователя."""


class Vote(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"
    NONE = "none"  # снять отметку


class TopicForm(BaseModel):
    """Название и запрос темы."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: Annotated[str, StringConstraints(min_length=1, max_length=NAME_MAX)] = Field(title="Название")
    query: Annotated[str, StringConstraints(max_length=QUERY_MAX)] = Field(
        "", title="Слова запроса",
        description="Сюжеты, где есть все эти слова (в любой форме), попадают в тему. "
                    "Можно оставить пустым: тогда в тему попадает похожее на добавленные сюжеты.",
    )


@dataclass(frozen=True, slots=True)
class TopicRules:
    min_similarity: float          # сходство с 👍-примером, с которого сюжет попадает в тему (порог по умолчанию)
    dislike_min_similarity: float  # сходство с 👎-примером, с которого сюжет убирается (если он не ближе к 👍)
    backfill: timedelta            # за сколько дней пересчитать сюжеты после изменения темы
    # В каких пределах порог темы подстраивается под отметки пользователя (calibrate_threshold)
    similarity_floor: float = 0.83
    similarity_ceiling: float = 0.95

    @classmethod
    def from_settings(cls, config: Settings) -> Self:
        return cls(
            config.topic_example_min_similarity,
            config.topic_dislike_min_similarity,
            timedelta(days=config.topic_backfill_days),
            config.topic_similarity_floor,
            config.topic_similarity_ceiling,
        )

    def threshold(self, topic: Topic) -> float:
        """Порог похожести этой темы: подстроенный по отметкам или общий."""
        return topic.min_similarity if topic.min_similarity is not None else self.min_similarity


# Шаг подстройки над 👎-примером: порог встаёт чуть выше его похожести, чтобы такие не проходили
THRESHOLD_STEP = 0.005


def calibrate_threshold(
    default: float, likes: Collection[float], dislikes: Collection[float], floor: float, ceiling: float,
) -> float:
    """Порог похожести темы по отметкам пользователя (подстройка порога, как в адаптивной фильтрации TREC).

    likes и dislikes — насколько каждый отмеченный сюжет похож на другие 👍-примеры темы.
    👍 ниже порога значит «тема слишком строгая»: такой сюжет она сама бы не нашла. 👎 выше порога —
    «слишком широкая»: сюжет прошёл по похожести, а он не нужен. Порог выбирается так, чтобы
    ошибок по отметкам было меньше всего; при равенстве — ближе к общему.

    Отметки ниже floor не учитываются: такой 👍 — это другое направление темы, а не сигнал опустить
    порог для всего, а такой 👎 тема и так бы не взяла. Порог не выходит за [floor, ceiling].
    """
    likes = [s for s in likes if s >= floor]
    dislikes = [s for s in dislikes if s >= floor]
    if not likes and not dislikes:
        return default
    candidates = {default, *likes, *(s + THRESHOLD_STEP for s in dislikes)}
    candidates = {min(max(c, floor), ceiling) for c in candidates}

    def cost(threshold: float) -> tuple[int, float]:
        errors = sum(s < threshold for s in likes) + sum(s >= threshold for s in dislikes)
        return errors, abs(threshold - default)

    best: float = min(candidates, key=cost)
    return round(best, 4)


# Тема из сюжета называется началом его заголовка: название живёт на вкладке и должно быть коротким
STORY_NAME_MAX = 40


def topic_name_from(text: str, max_chars: int = NAME_MAX) -> str:
    """Название по запросу или заголовку сюжета: не длиннее max_chars, без обрезанного слова."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars - 1)
    return text[: cut if cut > 0 else max_chars - 1].rstrip(" ,:;—-") + "…"


class TopicService:
    def __init__(self, session: AsyncSession, rules: TopicRules) -> None:
        self.session = session
        self.rules = rules
        self.topics = TopicRepository(session)

    async def create(
        self, user: User, name: str, query: str | None,
        likes: Collection[int] = (), dislikes: Collection[int] = (),
    ) -> Topic:
        """Новая тема. likes и dislikes — сюжеты, отмеченные 👍/👎 в поиске: тема сразу начинает с них.

        Сразу собирает подходящие сюжеты за последние дни.
        """
        topic = self.topics.add(user.id, name, query or None, secrets.token_urlsafe(24))
        await self.session.flush()
        both = set(likes) & set(dislikes)  # отмечен и так и так — считаем, что не отмечен
        for story_id, vote in [*((i, Vote.LIKE) for i in likes), *((i, Vote.DISLIKE) for i in dislikes)]:
            if story_id in both:
                continue
            try:
                await self._set_vote(topic, story_id, vote)
            except TopicError:
                continue  # сюжет исчез или ещё без вектора — без этого примера
        await self.relearn(topic)
        return topic

    async def follow_story(self, user: User, story_id: int) -> Topic:
        """«Следить за такими»: новая тема, первый 👍-пример — этот сюжет."""
        story = await StoryRepository(self.session).get(story_id)
        if story is None:
            raise TopicError("Сюжет не найден")
        topic = self.topics.add(user.id, topic_name_from(story.title, STORY_NAME_MAX), None, secrets.token_urlsafe(24))
        await self.session.flush()
        await self._set_vote(topic, story_id, Vote.LIKE)
        await self.relearn(topic)
        return topic

    async def vote(self, user: User, topic_id: int, story_id: int, vote: Vote) -> MatchResult:
        """👍/👎 на сюжете в теме (или снять отметку). Тема сразу пересчитывается."""
        topic = await self.owned(user, topic_id, lock=True)
        await self._set_vote(topic, story_id, vote)
        return await self.relearn(topic)

    async def remove_example(self, user: User, topic_id: int, example_id: int) -> MatchResult:
        topic = await self.owned(user, topic_id, lock=True)
        await self.topics.delete_example(topic.id, example_id)
        return await self.relearn(topic)

    async def update(self, user: User, topic_id: int, form: TopicForm) -> Topic:
        topic = await self.owned(user, topic_id, lock=True)
        query = form.query or None
        query_changed = query != topic.query
        topic.name, topic.query = form.name, query
        if query_changed:
            await self.rematch(topic)
        return topic

    async def set_paused(self, user: User, topic_id: int, paused: bool) -> Topic:
        """Пауза: тема не пополняется и не видна в ленте. После паузы догоняет пропущенное."""
        topic = await self.owned(user, topic_id, lock=True)
        topic.paused = paused
        if not paused:
            await self.rematch(topic)
        return topic

    async def delete(self, user: User, topic_id: int) -> str:
        topic = await self.owned(user, topic_id, lock=True)
        await self.session.delete(topic)
        return topic.name

    async def new_rss_token(self, user: User, topic_id: int) -> Topic:
        """Сменить адрес RSS-ленты: старый перестанет работать (если им кто-то поделился)."""
        topic = await self.owned(user, topic_id, lock=True)
        topic.rss_token = secrets.token_urlsafe(24)
        return topic

    async def owned(self, user: User, topic_id: int, *, lock: bool = False) -> Topic:
        topic = await self.topics.get_owned(topic_id, user.id, lock=lock)
        if topic is None:
            raise TopicError("Тема не найдена")
        return topic

    async def relearn(self, topic: Topic) -> MatchResult:
        """Примеры темы изменились: подстроить её порог и пересчитать совпадения."""
        likes, dislikes = await self.topics.example_similarities(topic.id)
        threshold = calibrate_threshold(
            self.rules.min_similarity, likes, dislikes, self.rules.similarity_floor, self.rules.similarity_ceiling,
        )
        topic.min_similarity = None if threshold == self.rules.min_similarity else threshold
        return await self.rematch(topic)

    async def rematch(self, topic: Topic, updated_after: datetime = NEVER) -> MatchResult:
        """Пересчитать совпадения темы за последние rules.backfill дней."""
        result = await self.topics.rematch(
            topic,
            updated_after=updated_after,
            active_after=datetime.now(UTC) - self.rules.backfill,
            min_similarity=self.rules.threshold(topic),
            dislike_min_similarity=self.rules.dislike_min_similarity,
        )
        if result.last_updated and (topic.matched_until is None or result.last_updated > topic.matched_until):
            topic.matched_until = result.last_updated
        return result

    async def _set_vote(self, topic: Topic, story_id: int, vote: Vote) -> None:
        example = await self.topics.example_for_story(topic.id, story_id)
        if vote is Vote.NONE:
            if example is not None:
                await self.session.delete(example)
                await self.session.flush()
            return
        story = await StoryRepository(self.session).get(story_id)
        if story is None:
            raise TopicError("Сюжет не найден")
        if story.centroid is None:
            raise TopicError("Сюжет ещё обрабатывается, отметьте его чуть позже")
        if example is None:
            example = TopicExample(topic_id=topic.id, story_id=story_id)
            self.session.add(example)
        # Копия вектора и заголовка: пример переживёт пересборку сюжетов
        example.liked = vote is Vote.LIKE
        example.title = story.title
        example.embedding = list(story.centroid)
        await self.session.flush()


@dataclass(frozen=True, slots=True)
class MatchBatchResult:
    topics: int = 0
    added: int = 0
    removed: int = 0

    @property
    def total(self) -> int:
        return self.topics

    def __str__(self) -> str:
        return f"тем={self.topics} новых совпадений={self.added} убрано={self.removed}"


class TopicMatchingService:
    """Для worker'а: сопоставить новые и обновлённые сюжеты со всеми активными темами."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], rules: TopicRules) -> None:
        self.session_factory = session_factory
        self.rules = rules

    async def process_batch(self) -> MatchBatchResult:
        async with self.session_factory() as session:
            topic_ids = await TopicRepository(session).active_ids_behind()
        added = removed = processed = 0
        for topic_id in topic_ids:
            # Каждая тема в своей транзакции: ошибка или блокировка одной не задерживает остальные
            try:
                async with self.session_factory() as session, session.begin():
                    topic = await TopicRepository(session).lock_for_matching(topic_id)
                    if topic is None:  # удалена, на паузе или её прямо сейчас меняет веб — в следующий раз
                        continue
                    since = topic.matched_until - MATCH_OVERLAP if topic.matched_until else NEVER
                    result = await TopicService(session, self.rules).rematch(topic, updated_after=since)
            except Exception:
                logger.exception("topic=%s: ошибка сопоставления с темой", topic_id, extra={"topic_id": topic_id})
                continue
            processed += 1
            added += result.added
            removed += result.removed
        batch = MatchBatchResult(processed, added, removed)
        if batch.added or batch.removed:
            logger.info("темы: %s", batch, extra=asdict(batch))
        return batch
