"""Шаг 4 пайплайна: превратить заголовок и начало текста поста в вектор."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import PostStatus
from sito.repositories.posts import PostRepository
from sito.services.pipeline import mark_post_done, record_post_error
from sito.utils.text import embedding_plain

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class Embedder(ABC):
    """Модель эмбеддингов. Сервису всё равно, что внутри: локальная модель или HTTP-сервер."""

    name: str  # сохраняется в post.embedding_model
    dim: int   # должен совпадать с размером колонки post.embedding

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Нормализованные векторы: косинусное сходство = скалярное произведение."""


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model: "SentenceTransformer", name: str, prefix: str, batch_size: int) -> None:
        self._model = model
        self.name = name
        self.dim = model.get_embedding_dimension() or 0
        self._prefix = prefix
        self._batch_size = batch_size

    @classmethod
    async def load(cls, name: str, prefix: str = "", batch_size: int = 32) -> Self:
        """Загрузить модель: первый раз она скачивается с Hugging Face (до гигабайта и больше)."""

        def _load() -> "SentenceTransformer":
            # Импорт здесь: sentence_transformers тянет torch, это несколько секунд
            from sentence_transformers import SentenceTransformer

            return SentenceTransformer(name, device="cpu")

        return cls(await asyncio.to_thread(_load), name, prefix, batch_size)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Расчёт занимает процессор: в отдельном потоке, иначе замрут остальные циклы worker'а.
        # torch во время вычислений отпускает GIL, так что опрос лент и очистка продолжают работать.
        vectors = await asyncio.to_thread(
            self._model.encode,
            [self._prefix + text for text in texts],
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()


class LazyEmbedder(Embedder):
    """Модель, которая загружается при первом обращении: для веба, где она нужна только поиску."""

    def __init__(self, load: Callable[[], Awaitable[Embedder]], name: str, dim: int) -> None:
        self._load = load
        self._embedder: Embedder | None = None
        self._lock = asyncio.Lock()
        self.name = name
        self.dim = dim

    async def get(self) -> Embedder:
        async with self._lock:  # два первых поиска одновременно не грузят модель дважды
            if self._embedder is None:
                self._embedder = await self._load()
            return self._embedder

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await (await self.get()).embed(texts)


def embedding_text(title: str, text: str | None, max_chars: int = 1000) -> str:
    """Заголовок и начало текста (как в ТЗ). Главное в новости всегда в начале.

    Без эмодзи, хэштегов и ссылок и не дальше пары абзацев: так пост Telegram сравним с новостью из RSS.
    """
    body = embedding_plain(text or "")
    if len(body) > max_chars:
        cut = body.rfind(" ", 0, max_chars)  # не резать слово пополам
        body = body[: cut if cut > 0 else max_chars]
    return f"{title}\n{body}".strip()


@dataclass(frozen=True, slots=True)
class EmbedBatchResult:
    embedded: int = 0
    errors: int = 0

    @property
    def total(self) -> int:
        return self.embedded + self.errors

    def __str__(self) -> str:
        return f"векторов={self.embedded} ошибок={self.errors}"


class EmbeddingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        batch_size: int = 32,
        max_chars: int = 1000,
    ) -> None:
        self.session_factory = session_factory
        self.embedder = embedder
        self.batch_size = batch_size
        self.max_chars = max_chars

    async def process_batch(self) -> EmbedBatchResult:
        """Считает векторы для одной пачки очищенных постов. Пустой результат — работы нет."""
        async with self.session_factory() as session, session.begin():
            posts = await PostRepository(session).take_pending(PostStatus.CLEANED, self.batch_size)
            if not posts:
                return EmbedBatchResult()
            try:
                vectors = await self.embedder.embed([embedding_text(p.title, p.text, self.max_chars) for p in posts])
                if len(vectors) != len(posts):
                    raise RuntimeError(f"Модель вернула {len(vectors)} векторов на {len(posts)} текстов")
            except Exception as exc:
                # Модель обрабатывает пачку целиком: ошибка засчитывается всем постам пачки
                for post in posts:
                    record_post_error(post, exc)
                logger.exception("ошибка расчёта векторов для пачки из %d постов", len(posts))
                result = EmbedBatchResult(errors=len(posts))
            else:
                for post, vector in zip(posts, vectors, strict=True):
                    post.embedding = vector
                    post.embedding_model = self.embedder.name
                    mark_post_done(post, PostStatus.EMBEDDED)
                result = EmbedBatchResult(embedded=len(posts))

        logger.info("эмбеддинги: %s", result, extra=asdict(result))
        return result
