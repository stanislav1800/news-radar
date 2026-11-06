"""Шаг 3 пайплайна: очистить текст новых постов, достать ссылки, отсеять мусор."""

import logging
from dataclasses import asdict, dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus
from sito.models.post import LEMMA_TEXT_MAX_CHARS
from sito.repositories.posts import PostRepository
from sito.services.pipeline import MAX_ATTEMPTS, mark_post_done, record_post_error
from sito.utils.lemmas import lemma_text
from sito.utils.text import (
    clean_html,
    clean_line,
    is_chatter,
    split_headline,
    tidy_title,
    title_from_text,
)

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


def set_lemmas(post: Post) -> None:
    """Слова поста в начальной форме для поиска: заголовок и начало текста."""
    post.title_lemmas = lemma_text(post.title)
    post.text_lemmas = lemma_text((post.text or "")[:LEMMA_TEXT_MAX_CHARS])


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
        raw, title = post.raw_text, clean_line(post.title)
        derived = not title  # у поста нет своего заголовка — Telegram
        headline = None
        if not title and raw and (split := split_headline(raw)):
            # Пост без заголовка (Telegram), но первая строка явно заголовок: жирная или отделена пустой строкой
            headline, raw = split
        cleaned = clean_html(raw, own_url=post.url_normalized)
        if headline is not None:
            cleaned = replace(cleaned, links=[*clean_html(headline, own_url=post.url_normalized).links, *cleaned.links])
            title = tidy_title(clean_line(headline))
        if derived and not title:
            title = tidy_title(title_from_text(cleaned.text))  # первая фраза сплошного текста
        post.title = title
        post.text = cleaned.text or None
        post.links = cleaned.links
        set_lemmas(post)

        # Короткий пост без текста — не мусор: для склейки хватит заголовка. Но пост без своего заголовка
        # (Telegram), где нет ни имён, ни мест, ни чисел, — болтовня канала, а не новость
        chatter = derived and is_chatter(f"{post.title}\n{post.text or ''}")
        wordless = sum(ch.isalpha() for ch in f"{post.title}{post.text or ''}") < 3  # одни эмодзи и значки
        junk = cleaned.is_ad or chatter or wordless or not (post.title or post.text)
        mark_post_done(post, PostStatus.SKIPPED if junk else PostStatus.CLEANED)
        return post.status
