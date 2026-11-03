"""Полный текст статей RSS: в ленте у RSS только анонс, а выжимке LLM нужна статья целиком.

Страница скачивается и из неё достаётся текст статьи (trafilatura: без меню, рекламы и комментариев).
На склейку и поиск не влияет: они работают по заголовку и анонсу. Только свежие посты: старые уже
никому не нужны, а сайты не любят, когда их выкачивают.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import trafilatura
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus, Source, SourceType

logger = logging.getLogger(__name__)

MIN_CHARS = 300  # меньше — это не статья, а заглушка, «включите JavaScript» или тот же анонс


@dataclass(frozen=True, slots=True)
class FullTextResult:
    fetched: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.fetched + self.failed


def extract_article(html: str) -> str | None:
    text = trafilatura.extract(html, favor_precision=True, include_comments=False, include_tables=False)
    return text if text and len(text) >= MIN_CHARS else None


class FullTextService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], client: httpx.AsyncClient, *,
        batch_size: int = 10, concurrency: int = 4, max_age: timedelta = timedelta(days=1),
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.batch_size = batch_size
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_age = max_age

    async def process_batch(self) -> FullTextResult:
        async with self.session_factory() as session:
            rows = list(await session.execute(
                select(Post.id, Post.url).join(Source, Source.id == Post.source_id)
                .where(Source.type == SourceType.RSS, Post.full_text_at.is_(None),
                       Post.status != PostStatus.SKIPPED, Post.fetched_at > datetime.now(UTC) - self.max_age)
                .order_by(Post.id.desc()).limit(self.batch_size)
            ))
        if not rows:
            return FullTextResult()
        texts = await asyncio.gather(*(self._fetch(url) for _, url in rows))
        async with self.session_factory() as session, session.begin():
            for (post_id, _), text in zip(rows, texts, strict=True):
                await session.execute(
                    update(Post).where(Post.id == post_id).values(full_text=text, full_text_at=datetime.now(UTC))
                )
        fetched = sum(t is not None for t in texts)
        return FullTextResult(fetched, len(texts) - fetched)

    async def _fetch(self, url: str) -> str | None:
        async with self.semaphore:
            try:
                response = await self.client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.debug("полный текст: %s не скачался: %s", url, exc)
                return None
        if "html" not in response.headers.get("content-type", "html"):
            return None
        try:
            return await asyncio.to_thread(extract_article, response.text)
        except Exception:
            logger.warning("полный текст: не разобрать %s", url, exc_info=True)
            return None
