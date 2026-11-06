"""Поиск по сюжетам: по словам и по смыслу."""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from weakref import WeakKeyDictionary

from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import User
from sito.repositories.stories import FeedStory, StoryRepository
from sito.services.embedding import Embedder
from sito.utils.lemmas import parse_query

logger = logging.getLogger(__name__)

PERIODS = {1: "Сутки", 7: "Неделя", 30: "Месяц", 0: "Всё время"}
DEFAULT_PERIOD = 0  # за всё время: свежие и так выше (см. _SEARCH_WORDS_SQL)
# Ниже этого сходство запроса с сюжетом — уже не про запрос. Короткий запрос похож на сюжеты
# слабее, чем сюжеты друг на друга: у релевантных обычно 0.79–0.87
MEANING_MIN_SIMILARITY = 0.78


def meaning_text(query: str) -> str:
    """Запрос для модели: без синтаксиса поиска по словам (or, -слово, кавычки) — модели он только мешает."""
    words = [w.strip('"') for w in query.split() if w.lower() != "or" and not w.startswith("-")]
    return " ".join(w for w in words if w) or query


MEANING_LIMIT = 10  # «по смыслу» — продолжение списка, а не его основа: модель плохо понимает короткие запросы


@dataclass(frozen=True, slots=True)
class SearchHit:
    story: FeedStory
    by_meaning: bool  # найден по смыслу, слов запроса в нём нет


@dataclass(frozen=True, slots=True)
class SearchResults:
    query: str
    period: int
    hits: list[SearchHit] = field(default_factory=list)
    meaning_unavailable: bool = False  # модель не загрузилась или выключена: только по словам


# Векторы запросов: лента темы считает вектор её запроса при каждом открытии, а модель на процессоре
# тратит на это десятки миллисекунд. Запросы тем повторяются — помним последние
QUERY_CACHE_SIZE = 512
_query_vectors: WeakKeyDictionary[Embedder, OrderedDict[str, list[float]]] = WeakKeyDictionary()


class SearchService:
    def __init__(
        self, session: AsyncSession, embedder: Embedder | None, limit: int, retention_days: int | None = None
    ) -> None:
        self.session = session
        self.embedder = embedder
        self.limit = limit
        # «За всё время» по смыслу — только в пределах срока хранения: у более старых сюжетов
        # тексты уже убраны, а перебор всех векторов с годами становится всё дольше
        self.retention_days = retention_days

    async def embed_query(self, query: str) -> list[float] | None:
        """Вектор запроса или None, если поиск по смыслу недоступен. Ошибка модели не ломает поиск."""
        if self.embedder is None:
            return None
        key = meaning_text(query)
        cache = _query_vectors.setdefault(self.embedder, OrderedDict())
        if (vector := cache.get(key)) is not None:
            cache.move_to_end(key)
            return vector
        try:
            [vector] = await self.embedder.embed([key])
        except Exception:
            logger.exception("поиск по смыслу недоступен: модель не посчитала вектор запроса")
            return None
        cache[key] = vector
        if len(cache) > QUERY_CACHE_SIZE:
            cache.popitem(last=False)
        return vector

    async def search(self, user: User, query: str, period: int) -> SearchResults:
        """Один список: сначала сюжеты со словами запроса (в любой форме, самые подходящие сверху),
        затем близкие по смыслу без этих слов. Пользователь пишет обычным текстом."""
        query = " ".join(query.split())
        period = period if period in PERIODS else DEFAULT_PERIOD
        if not query:
            return SearchResults(query, period)
        now = datetime.now(UTC)
        since = now - timedelta(days=period) if period else None
        meaning_since = since or (now - timedelta(days=self.retention_days) if self.retention_days else None)
        stories = StoryRepository(self.session)
        parsed = parse_query(query)

        word_ids = await stories.search_words(parsed.words, parsed.excluded, since, self.limit)
        vector = await self.embed_query(query)
        meaning_ids = (
            await stories.search_meaning(vector, meaning_since, MEANING_LIMIT, MEANING_MIN_SIMILARITY, exclude=word_ids)
            if vector is not None else []
        )
        cards = {
            card.id: card for card in await stories.cards(
                [*word_ids, *meaning_ids], user_id=user.id, read_all_before=user.read_all_before
            )
        }
        hits = [SearchHit(cards[i], False) for i in word_ids if i in cards]
        hits += [SearchHit(cards[i], True) for i in meaning_ids if i in cards]
        return SearchResults(query, period, hits, meaning_unavailable=vector is None)
