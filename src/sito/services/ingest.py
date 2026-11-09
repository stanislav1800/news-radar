import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito import metrics
from sito.models import ConnectorStatus, Source, SourceType
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.posts import NewPost, PostRepository
from sito.repositories.sources import SourceRepository
from sito.services.collector.base import (
    BaseCollector,
    Command,
    KnownSource,
    PendingSource,
    RawPost,
    SaveResult,
)
from sito.utils.urls import content_hash, normalize_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult(SaveResult):
    """Итог опроса источника: что сохранилось плюс сколько пришло."""

    fetched: int = 0            # пришло из источника
    not_modified: bool = False  # лента не менялась (HTTP 304)
    skipped: bool = False       # источник выключен

    def __str__(self) -> str:
        if self.skipped:
            return "выключен, пропущен"
        if self.not_modified:
            return "не менялся (304)"
        return f"получено={self.fetched} {SaveResult.__str__(self)}"


def _known(source: Source) -> KnownSource:
    return KnownSource(
        id=source.id, external_id=source.external_id or "", name=source.name,
        enabled=source.enabled, state=dict(source.state or {}), info=dict(source.info or {}),
    )


class IngestService:
    """Шаги 1–2: сбор и сохранение постов. Для слушающих сборщиков — их PostSink."""

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

        if fetched.not_modified:
            async with self.session_factory() as session, session.begin():
                await SourceRepository(session).mark_success(source_id, fetched.state)
            return IngestResult(not_modified=True)

        saved = await self.save(source_id, fetched.posts, fetched.state)
        return IngestResult(fetched=len(fetched.posts), **asdict(saved))

    # --- PostSink: общее сохранение для опрашивающих и слушающих сборщиков ---------------------

    async def save(
        self, source_id: int, posts: Sequence[RawPost], state: Mapping[str, Any] | None = None
    ) -> SaveResult:
        """Сохранить посты источника и его курсор одной транзакцией: курсор не убежит вперёд постов."""
        async with self.session_factory() as session, session.begin():
            prepared, invalid = self._prepare(posts)
            fresh = await self._drop_duplicates(PostRepository(session), prepared)
            saved = await PostRepository(session).upsert_many(fresh) if fresh else None
            await SourceRepository(session).mark_success(source_id, state)
        result = SaveResult(
            inserted=saved.inserted if saved else 0,
            updated=saved.updated if saved else 0,
            unchanged=saved.unchanged if saved else 0,
            duplicates=len(prepared) - len(fresh),
            invalid=invalid,
        )
        for name, count in asdict(result).items():
            if count:
                metrics.POSTS_SAVED.labels(name).inc(count)
        return result

    async def sources(self, connector_id: int) -> list[KnownSource]:
        async with self.session_factory() as session:
            found = await SourceRepository(session).list_for_connector(connector_id)
        return [_known(s) for s in found if s.external_id is not None]

    async def source(
        self, connector_id: int, external_id: str, *, name: str, url: str | None = None, enabled: bool = True,
    ) -> KnownSource:
        async with self.session_factory() as session, session.begin():
            connector = await ConnectorRepository(session).get(connector_id)
            if connector is None:
                raise LookupError(f"Подключение {connector_id} не найдено")
            found = await SourceRepository(session).get_or_add_external(
                connector_id, connector.type, external_id, name, url, enabled=enabled,
            )
            return _known(found)

    async def connected(self, connector_id: int) -> None:
        async with self.session_factory() as session, session.begin():
            await ConnectorRepository(session).set_status(connector_id, ConnectorStatus.ACTIVE)

    async def pending_sources(self, connector_id: int) -> list[PendingSource]:
        async with self.session_factory() as session:
            found = await SourceRepository(session).pending(connector_id)
        return [PendingSource(s.id, s.url) for s in found if s.url]

    async def bind_pending(self, source_id: int, external_id: str, *, name: str) -> KnownSource:
        async with self.session_factory() as session, session.begin():
            found = await SourceRepository(session).bind_pending(source_id, external_id, name)
            if found is None:
                raise LookupError(f"Источник {source_id} не найден")
            return _known(found)

    async def pending_failed(self, source_id: int, error: str) -> None:
        async with self.session_factory() as session, session.begin():
            await SourceRepository(session).mark_failed(source_id, error)

    async def update_info(self, source_id: int, info: Mapping[str, Any]) -> None:
        async with self.session_factory() as session, session.begin():
            await SourceRepository(session).update_info(source_id, info)

    async def forget_source(self, source_id: int, *, delete: bool) -> None:
        async with self.session_factory() as session, session.begin():
            repo = SourceRepository(session)
            if delete:
                await repo.delete(source_id)
            else:
                await repo.set_enabled(source_id, False)
                await repo.update_info(source_id, {"left": True, "leaving": False})

    async def remove_posts(self, source_id: int, external_ids: Sequence[str]) -> None:
        async with self.session_factory() as session, session.begin():
            removed = await SourceRepository(session).delete_posts(source_id, external_ids)
        if removed:
            logger.info("source=%s: источник удалил постов: %d", source_id, removed)

    async def next_command(self, connector_id: int) -> Command | None:
        async with self.session_factory() as session, session.begin():
            found = await ConnectorRepository(session).take_command(connector_id)
            return Command(found.id, found.kind, dict(found.payload)) if found else None

    async def fail_interrupted_commands(self, connector_id: int) -> None:
        async with self.session_factory() as session, session.begin():
            await ConnectorRepository(session).fail_interrupted(connector_id)

    async def finish_command(self, command_id: int, result: Any = None, error: str | None = None) -> None:
        async with self.session_factory() as session, session.begin():
            await ConnectorRepository(session).finish_command(command_id, result, error)

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
        urls = {p.url_normalized for p in posts}
        await repo.lock_urls(urls)
        owners = await repo.url_owners(urls)
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
