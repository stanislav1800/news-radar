import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import SourceType
from sito.repositories.posts import NewPost, PostRepository
from sito.repositories.sources import SourceRepository
from sito.services.collector.base import BaseCollector, RawPost
from sito.utils.urls import content_hash, normalize_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    fetched: int = 0      # пришло из источника
    invalid: int = 0      # без нормального адреса
    duplicates: int = 0   # статья уже есть у другого поста
    inserted: int = 0
    updated: int = 0      # источник отредактировал пост
    unchanged: int = 0
    not_modified: bool = False  # лента не менялась (HTTP 304)
    skipped: bool = False       # источник выключен

    def __str__(self) -> str:
        if self.skipped:
            return "выключен, пропущен"
        if self.not_modified:
            return "не менялся (304)"
        return (
            f"получено={self.fetched} новых={self.inserted} обновлено={self.updated} "
            f"без изменений={self.unchanged} дублей={self.duplicates} без адреса={self.invalid}"
        )


class IngestService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        collectors: Mapping[SourceType, BaseCollector],
        fetch_timeout: float = 60,
    ) -> None:
        self.session_factory = session_factory
        self.collectors = collectors
        self.fetch_timeout = fetch_timeout

    async def ingest_source(self, source_id: int) -> IngestResult:
        """Опрашивает один источник. При ошибке записывает её в источник и пробрасывает дальше."""
        try:
            result = await self._ingest(source_id)
        except Exception as exc:
            await self._mark_failed(source_id, exc)
            raise
        logger.info("source=%s %s", source_id, result, extra={"source_id": source_id, **asdict(result)})
        return result

    async def _ingest(self, source_id: int) -> IngestResult:
        async with self.session_factory() as session:
            source = await SourceRepository(session).get(source_id)
        if source is None:
            raise LookupError(f"Источник {source_id} не найден")
        if not source.enabled:
            return IngestResult(skipped=True)

        collector = self.collectors.get(source.type)
        if collector is None:
            raise LookupError(f"Нет коллектора для типа {source.type!r}")

        async with asyncio.timeout(self.fetch_timeout):
            fetched = await collector.fetch(source)

        async with self.session_factory() as session, session.begin():
            sources = SourceRepository(session)

            if fetched.not_modified:
                await sources.mark_success(source_id, fetched.etag, fetched.last_modified)
                return IngestResult(not_modified=True)

            prepared, invalid = self._prepare(fetched.posts)
            fresh = await self._drop_duplicates(PostRepository(session), prepared)
            saved = await PostRepository(session).upsert_many(fresh)
            await sources.mark_success(source_id, fetched.etag, fetched.last_modified)

        return IngestResult(
            fetched=len(fetched.posts),
            invalid=invalid,
            duplicates=len(prepared) - len(fresh),
            inserted=saved.inserted,
            updated=saved.updated,
            unchanged=saved.unchanged,
        )

    @staticmethod
    def _prepare(raw_posts: Iterable[RawPost]) -> tuple[list[NewPost], int]:
        """RawPost → NewPost: нормализованный адрес и отпечаток содержимого."""
        posts: list[NewPost] = []
        invalid = 0
        for raw in raw_posts:
            try:
                url_normalized = normalize_url(raw.url)
            except ValueError:
                invalid += 1
                continue
            posts.append(NewPost(
                source_id=raw.source_id,
                external_id=raw.external_id,
                url=raw.url,
                url_normalized=url_normalized,
                title=raw.title,
                raw_text=raw.text,
                content_hash=content_hash(raw.title, raw.text),
                published_at=raw.published_at,
            ))
        return posts, invalid

    @staticmethod
    async def _drop_duplicates(repo: PostRepository, posts: list[NewPost]) -> list[NewPost]:
        """Отбрасывает посты, чья статья уже сохранена под другим постом (шаг 2 ТЗ).

        Тот же пост того же источника не отбрасывается: это обновление, его обработает upsert.
        """
        owners = await repo.url_owners({p.url_normalized for p in posts})
        fresh: list[NewPost] = []
        for post in posts:
            key = (post.source_id, post.external_id)
            if owners.setdefault(post.url_normalized, key) == key:
                fresh.append(post)
        return fresh

    async def _mark_failed(self, source_id: int, exc: Exception) -> None:
        """Ошибку пишем отдельной транзакцией: основная уже откатилась."""
        try:
            async with self.session_factory() as session, session.begin():
                await SourceRepository(session).mark_failed(source_id, f"{type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("source=%s: не удалось записать ошибку", source_id, extra={"source_id": source_id})
