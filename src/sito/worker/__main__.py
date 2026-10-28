import asyncio
import contextlib
import logging
import signal

import httpx

from sito.config import settings
from sito.db.engine import async_session, engine
from sito.log import setup_logging
from sito.models import SourceType
from sito.repositories.sources import SourceRepository
from sito.services.collector.rss import RssCollector
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


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    semaphore = asyncio.Semaphore(settings.worker_concurrency)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.http_user_agent},
    ) as client:
        service = IngestService(
            async_session,
            {SourceType.RSS: RssCollector(client)},
            fetch_timeout=settings.fetch_timeout,
        )
        logger.info("worker запущен")

        while not stop.is_set():
            try:
                await poll_due_sources(service, semaphore)
            except Exception:
                logger.exception("ошибка круга опроса")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_tick_seconds)

    await engine.dispose()
    logger.info("worker остановлен")


def run() -> None:
    setup_logging(settings.log_level, settings.log_format, settings.log_sql)
    asyncio.run(main())


if __name__ == "__main__":
    run()
