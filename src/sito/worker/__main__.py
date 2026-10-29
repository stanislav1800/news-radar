import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Protocol

import httpx

from sito.config import settings
from sito.db.engine import async_session, engine
from sito.log import setup_logging
from sito.models import SourceType
from sito.models.post import EMBEDDING_DIM
from sito.repositories.sources import SourceRepository
from sito.services.cleaning import CleaningService
from sito.services.clustering import ClusteringRules, ClusteringService
from sito.services.collector.rss import RssCollector
from sito.services.embedding import EmbeddingService, SentenceTransformerEmbedder
from sito.services.ingest import IngestService

logger = logging.getLogger("sito.worker")

POLLED_TYPES = (SourceType.RSS,)


async def poll_one(service: IngestService, semaphore: asyncio.Semaphore, source_id: int) -> None:
    """Опрашивает один источник. Никогда не падает: ошибка одного не мешает остальным."""
    async with semaphore:
        try:
            await service.ingest_source(source_id)
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning(
                "source=%s: %s: %s", source_id, type(exc).__name__, exc,
                extra={"source_id": source_id, "error": type(exc).__name__},
            )
        except Exception:
            logger.exception("source=%s: непредвиденная ошибка", source_id, extra={"source_id": source_id})


async def poll_due_sources(service: IngestService, semaphore: asyncio.Semaphore) -> None:
    """Один круг: найти источники, которым пора, и опросить их."""
    async with async_session() as session:
        source_ids = await SourceRepository(session).due_for_polling(POLLED_TYPES)
    if not source_ids:
        return
    logger.info("к опросу источников: %d", len(source_ids))
    await asyncio.gather(*(poll_one(service, semaphore, sid) for sid in source_ids))


async def sleep_until_stop(stop: asyncio.Event, seconds: float) -> None:
    """Пауза, которую прерывает сигнал остановки."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def poll_loop(service: IngestService, stop: asyncio.Event) -> None:
    """Шаги 1–2: опрос источников по расписанию."""
    semaphore = asyncio.Semaphore(settings.worker_concurrency)
    while not stop.is_set():
        try:
            await poll_due_sources(service, semaphore)
        except Exception:
            logger.exception("ошибка круга опроса")
        await sleep_until_stop(stop, settings.worker_tick_seconds)


class BatchResult(Protocol):
    @property
    def total(self) -> int: ...


async def drain_loop(
    name: str, process_batch: Callable[[], Awaitable[BatchResult]], tick: float, stop: asyncio.Event
) -> None:
    """Обрабатывать пачки подряд, пока есть работа, потом ждать tick секунд."""
    while not stop.is_set():
        try:
            result = await process_batch()
        except Exception:
            logger.exception("ошибка шага «%s»", name)
        else:
            if result.total:
                continue
        await sleep_until_stop(stop, tick)


async def embed_loop(stop: asyncio.Event) -> None:
    """Шаг 4: эмбеддинги. Сначала загрузить модель, при неудаче повторить через минуту."""
    while not stop.is_set():
        logger.info("загрузка модели %s (первый раз скачивается с Hugging Face)", settings.embedding_model)
        try:
            embedder = await SentenceTransformerEmbedder.load(
                settings.embedding_model, settings.embedding_prefix, settings.embedding_batch_size
            )
        except Exception:
            logger.exception("модель не загрузилась, повтор через минуту")
            await sleep_until_stop(stop, 60)
            continue
        if embedder.dim != EMBEDDING_DIM:
            # Ошибка настройки, а не сбой: повтор не поможет
            logger.error(
                "модель %s выдаёт векторы размера %d, а колонка post.embedding — %d. "
                "Нужна другая модель или миграция; эмбеддинги выключены.",
                embedder.name, embedder.dim, EMBEDDING_DIM,
            )
            return
        logger.info("модель загружена: %s, размер вектора %d", embedder.name, embedder.dim)
        service = EmbeddingService(
            async_session, embedder, settings.embedding_batch_size, settings.embedding_max_chars
        )
        await drain_loop("эмбеддинги", service.process_batch, settings.embedding_tick_seconds, stop)
        return


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.http_user_agent},
    ) as client:
        ingest = IngestService(
            async_session,
            {SourceType.RSS: RssCollector(client)},
            fetch_timeout=settings.fetch_timeout,
        )
        cleaning = CleaningService(async_session, batch_size=settings.clean_batch_size)
        clustering = ClusteringService(
            async_session,
            ClusteringRules(
                cross_source_min_similarity=settings.cluster_cross_source_min_similarity,
                same_source_min_similarity=settings.cluster_same_source_min_similarity,
                max_gap=timedelta(hours=settings.cluster_max_gap_hours),
            ),
            batch_size=settings.cluster_batch_size,
        )
        logger.info("worker запущен")

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(poll_loop(ingest, stop))
            tasks.create_task(drain_loop("очистка", cleaning.process_batch, settings.clean_tick_seconds, stop))
            tasks.create_task(embed_loop(stop))
            tasks.create_task(
                drain_loop("склейка", clustering.process_batch, settings.cluster_tick_seconds, stop)
            )

    await engine.dispose()
    logger.info("worker остановлен")


def run() -> None:
    setup_logging(settings.log_level, settings.log_format, settings.log_sql)
    asyncio.run(main())


if __name__ == "__main__":
    run()
