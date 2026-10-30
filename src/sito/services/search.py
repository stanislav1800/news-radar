"""Поиск по сюжетам: по словам и по смыслу."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import User
from sito.repositories.stories import FeedStory, StoryRepository
from sito.services.embedding import Embedder

logger = logging.getLogger(__name__)

PERIODS = {1: "Сутки", 7: "Неделя", 30: "Месяц", 0: "Всё время"}
DEFAULT_PERIOD = 7
# Ниже этого сходство запроса с сюжетом — уже не про запрос. Короткий запрос похож на сюжеты
# слабее, чем сюжеты друг на друга: у релевантных обычно 0.79–0.87
MEANING_MIN_SIMILARITY = 0.78


def meaning_text(query: str) -> str:
    """Запрос для модели: без синтаксиса поиска по словам (or, -слово, кавычки) — модели он только мешает."""
    words = [w.strip('"') for w in query.split() if w.lower() != "or" and not w.startswith("-")]
    return " ".join(w for w in words if w) or query


@dataclass(frozen=True, slots=True)
class SearchResults:
    query: str
    period: int
    by_words: list[FeedStory] = field(default_factory=list)
    by_meaning: list[FeedStory] = field(default_factory=list)
    meaning_unavailable: bool = False  # модель не загрузилась или выключена: только по словам


class SearchService:
    def __init__(self, session: AsyncSession, embedder: Embedder | None, limit: int) -> None:
        self.session = session
        self.embedder = embedder
        self.limit = limit

    async def embed_query(self, query: str) -> list[float] | None:
        """Вектор запроса или None, если поиск по смыслу недоступен. Ошибка модели не ломает поиск."""
        if self.embedder is None:
            return None
        try:
            [vector] = await self.embedder.embed([meaning_text(query)])
        except Exception:
            logger.exception("поиск по смыслу недоступен: модель не посчитала вектор запроса")
            return None
        return vector

    async def search(self, user: User, query: str, period: int) -> SearchResults:
        query = " ".join(query.split())
        period = period if period in PERIODS else DEFAULT_PERIOD
        if not query:
            return SearchResults(query, period)
        since = datetime.now(UTC) - timedelta(days=period) if period else None
        stories = StoryRepository(self.session)

        word_ids = await stories.search_words(query, since, self.limit)
        vector = await self.embed_query(query)
        meaning_ids = (
            await stories.search_meaning(vector, since, self.limit, MEANING_MIN_SIMILARITY, exclude=word_ids)
            if vector is not None else []
        )
        return SearchResults(
            query, period,
            by_words=await stories.cards(word_ids, user_id=user.id, read_all_before=user.read_all_before),
            by_meaning=await stories.cards(meaning_ids, user_id=user.id, read_all_before=user.read_all_before),
            meaning_unavailable=vector is None,
        )
