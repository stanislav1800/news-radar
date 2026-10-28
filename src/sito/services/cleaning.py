"""Шаг 3 пайплайна: очистить текст новых постов, достать ссылки, отсеять мусор."""

import logging
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus
from sito.repositories.posts import PostRepository
from sito.utils.text import clean_html, clean_line

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
ERROR_MAX_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class CleanBatchResult:
    cleaned: int = 0
    skipped: int = 0  # реклама или пустой пост
    errors: int = 0   # повторится позже, после MAX_ATTEMPTS — статус failed

    @property
    def total(self) -> int:
        return self.cleaned + self.skipped + self.errors

    def __str__(self) -> str:
        return f"очищено={self.cleaned} пропущено={self.skipped} ошибок={self.errors}"


class CleaningService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], batch_size: int = 100) -> None:
        self.session_factory = session_factory
        self.batch_size = batch_size

    async def process_batch(self) -> CleanBatchResult:
        """Обрабатывает одну пачку постов со статусом new. Пустой результат — работы нет."""
        counts = {PostStatus.CLEANED: 0, PostStatus.SKIPPED: 0}
        errors = 0
        async with self.session_factory() as session, session.begin():
            for post in await PostRepository(session).take_pending(PostStatus.NEW, self.batch_size):
                try:
                    status = self._clean(post)
                except Exception as exc:
                    errors += 1
                    self._record_error(post, exc)
                    logger.exception(
                        "post=%s: ошибка очистки, попытка %d", post.id, post.attempt, extra={"post_id": post.id}
                    )
                else:
                    counts[status] += 1
            # Изменения объектов Post запишет commit при выходе из session.begin()

        result = CleanBatchResult(counts[PostStatus.CLEANED], counts[PostStatus.SKIPPED], errors)
        if result.total:
            logger.info("очистка: %s", result, extra=asdict(result))
        return result

    @staticmethod
    def _clean(post: Post) -> PostStatus:
        cleaned = clean_html(post.raw_text, own_url=post.url_normalized)
        post.title = clean_line(post.title)
        post.text = cleaned.text or None
        post.links = cleaned.links
        post.last_error = None

        # Короткий пост без текста — не мусор: для склейки хватит заголовка
        if cleaned.is_ad or not (post.title or post.text):
            post.status = PostStatus.SKIPPED
        else:
            post.status = PostStatus.CLEANED
        return post.status

    @staticmethod
    def _record_error(post: Post, exc: Exception) -> None:
        post.attempt += 1
        post.last_error = f"{type(exc).__name__}: {exc}"[:ERROR_MAX_LENGTH]
        if post.attempt >= MAX_ATTEMPTS:
            post.status = PostStatus.FAILED  # больше не берём, ждёт разбора
