"""Шаг 3 пайплайна: очистить текст новых постов, достать ссылки, отсеять мусор."""

import logging
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus
from sito.repositories.posts import PostRepository
from sito.services.pipeline import MAX_ATTEMPTS, mark_post_done, record_post_error
from sito.utils.text import clean_html, clean_line, title_from_text

__all__ = ["MAX_ATTEMPTS", "CleanBatchResult", "CleaningService"]

logger = logging.getLogger(__name__)


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
                    record_post_error(post, exc)
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
        post.title = clean_line(post.title) or title_from_text(cleaned.text)  # у Telegram заголовков нет
        post.text = cleaned.text or None
        post.links = cleaned.links

        # Короткий пост без текста — не мусор: для склейки хватит заголовка
        junk = cleaned.is_ad or not (post.title or post.text)
        mark_post_done(post, PostStatus.SKIPPED if junk else PostStatus.CLEANED)
        return post.status
