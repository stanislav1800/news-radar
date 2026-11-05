"""Полный текст статей: в ленте у RSS только анонс, а выжимке LLM нужна статья целиком.

Telegram: у короткого поста (каналы РИА, ТАСС, РБК дают фразу и ссылку на статью на своём сайте)
скачивается статья по ссылке. Ссылки на соцсети, мессенджеры и главные страницы не годятся.

Страница скачивается и из неё достаётся текст статьи (trafilatura: без меню, рекламы и комментариев).
На склейку и поиск не влияет: они работают по заголовку и анонсу. Только свежие посты: старые уже
никому не нужны, а сайты не любят, когда их выкачивают.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import trafilatura
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus, Source, SourceType
from sito.utils.http import get_limited
from sito.utils.urls import normalize_url

logger = logging.getLogger(__name__)

MAX_PAGE_BYTES = 3_000_000  # статья больше не бывает; ссылка на файл — не статья
PAGE_TIMEOUT = 30           # на всю загрузку страницы
MIN_CHARS = 300  # меньше — это не статья, а заглушка, «включите JavaScript» или тот же анонс


# Не статьи: соцсети, мессенджеры, видео, сокращатели, магазины приложений, донаты
NOT_ARTICLES = (
    "t.me", "telegram.me", "telegram.org", "max.ru", "vk.com", "vk.cc", "vk.ru", "ok.ru", "instagram.com",
    "tiktok.com", "youtube.com", "youtu.be", "dzen.ru", "zen.yandex.ru", "rutube.ru", "x.com", "twitter.com",
    "facebook.com", "wa.me", "whatsapp.com", "ya.cc", "clck.ru", "bit.ly", "goo.gl", "tinyurl.com",
    "boosty.to", "donationalerts.com", "apps.apple.com", "play.google.com", "rustore.ru", "music.yandex.ru",
    "disk.yandex.ru", "drive.google.com", "docs.google.com",
)


def article_link(links: list[str]) -> str | None:
    """Первая ссылка, похожая на статью: не соцсеть и не главная страница или профиль."""
    for link in links[:6]:
        parts = urlsplit(link)
        host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
        if not host or parts.scheme not in ("http", "https"):
            continue
        if any(host == domain or host.endswith("." + domain) for domain in NOT_ARTICLES):
            continue
        path = parts.path.strip("/")
        if "/" in path or re.search(r"\d{4,}", path) or len(path) >= 25 or path.endswith((".html", ".htm", ".shtml")):
            return link
    return None


@dataclass(frozen=True, slots=True)
class FullTextResult:
    fetched: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.fetched + self.failed


def extract_article(html: str | bytes) -> str | None:
    text = trafilatura.extract(html, favor_precision=True, include_comments=False, include_tables=False)
    return text if text and len(text) >= MIN_CHARS else None


class FullTextService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], client: httpx.AsyncClient, *,
        batch_size: int = 10, concurrency: int = 4, max_age: timedelta = timedelta(days=1),
        short_post_chars: int = 500,
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.batch_size = batch_size
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_age = max_age
        self.short_post_chars = short_post_chars
        self.hosts: dict[str, asyncio.Semaphore] = {}

    async def process_batch(self) -> FullTextResult:
        async with self.session_factory() as session:
            rows = list(await session.execute(
                select(Post.id, Post.url, Source.type, Post.links).join(Source, Source.id == Post.source_id)
                .where(
                    Post.full_text_at.is_(None), Post.fetched_at > datetime.now(UTC) - self.max_age,
                    or_(
                        (Source.type == SourceType.RSS) & (Post.status != PostStatus.SKIPPED),
                        # Telegram: ссылки появляются при очистке; только короткие посты со ссылками
                        (Source.type == SourceType.TELEGRAM)
                        & Post.status.not_in((PostStatus.NEW, PostStatus.SKIPPED))
                        & (func.cardinality(Post.links) > 0)
                        & (func.length(func.coalesce(Post.text, "")) < self.short_post_chars),
                    ),
                )
                .order_by(Post.id.desc()).limit(self.batch_size)
            ))
            urls = [url if kind == SourceType.RSS else article_link(list(links or ())) for _, url, kind, links in rows]
            # Статью уже принёс RSS того же издания — взять её текст, а не качать заново
            known = {normalize_url(url) for url in urls if url}
            ready = {url: text for url, text in await session.execute(
                select(Post.url_normalized, Post.full_text)
                .where(Post.url_normalized.in_(known), Post.full_text.is_not(None), Post.full_text != "")
            )} if known else {}
        if not rows:
            return FullTextResult()

        async def text_of(url: str | None) -> str | None:
            if url is None:
                return None
            return ready.get(normalize_url(url)) or await self._fetch(url)

        texts = await asyncio.gather(*(text_of(url) for url in urls))
        async with self.session_factory() as session, session.begin():
            for (post_id, *_), text in zip(rows, texts, strict=True):
                await session.execute(
                    update(Post).where(Post.id == post_id).values(full_text=text, full_text_at=datetime.now(UTC))
                )
        fetched = sum(t is not None for t in texts)
        return FullTextResult(fetched, len(texts) - fetched)

    async def _fetch(self, url: str) -> str | None:
        host = urlsplit(url).hostname or ""
        # Не больше двух запросов к одному сайту одновременно: десять статей ТАСС подряд — не повод его долбить
        per_host = self.hosts.setdefault(host, asyncio.Semaphore(2))
        async with self.semaphore, per_host:
            try:
                response, content = await get_limited(
                    self.client, url, max_bytes=MAX_PAGE_BYTES, timeout=PAGE_TIMEOUT,
                    accept=lambda headers: "html" in headers.get("content-type", "html"),
                )
                response.raise_for_status()
            except (httpx.HTTPError, TimeoutError) as exc:
                logger.debug("полный текст: %s не скачался: %s", url, exc)
                return None
        if not content:
            return None  # не страница (видео, PDF, архив) — тело и не читали
        try:
            # Байты, а не текст: кодировку страницы (cp1251 у части сайтов) trafilatura определит сама
            return await asyncio.to_thread(extract_article, content)
        except Exception:
            logger.warning("полный текст: не разобрать %s", url, exc_info=True)
            return None
