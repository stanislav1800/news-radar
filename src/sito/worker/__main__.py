import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from sito.config import settings
from sito.db.engine import async_session, engine
from sito.log import setup_logging
from sito.models import ConnectorStatus, SourceType
from sito.models.post import EMBEDDING_DIM
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.pipeline import PipelineRepository
from sito.repositories.sources import SourceRepository
from sito.repositories.stories import StoryRepository
from sito.services.alerts import Problem, alert_text, find_problems
from sito.services.cleaning import CleaningService
from sito.services.clustering import ClusteringRules, ClusteringService
from sito.services.collector.base import ConnectorListener
from sito.services.collector.rss import RssCollector
from sito.services.collector.telegram import TelegramListener, TelegramRules
from sito.services.collector.telegram_client import telethon_connect
from sito.services.connectors import ConnectorSupervisor
from sito.services.embedding import EmbeddingService, SentenceTransformerEmbedder
from sito.services.fulltext import FullTextService
from sito.services.ingest import IngestService
from sito.services.llm import (
    DigestService,
    LlmClient,
    StoryMergeService,
    TopicCheckService,
)
from sito.services.topics import TopicMatchingService, TopicRules
from sito.utils.crypto import SecretBox

logger = logging.getLogger("sito.worker")

POLLED_TYPES = (SourceType.RSS,)
# Слушающие сборщики по типам подключений
LISTENERS: dict[SourceType, ConnectorListener] = {
    SourceType.TELEGRAM: TelegramListener(
        telethon_connect(settings.telegram_flood_sleep_seconds),
        TelegramRules(catch_up=timedelta(hours=settings.telegram_catch_up_hours)),
    ),
}


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
        source_ids = await SourceRepository(session).due_for_polling(
            POLLED_TYPES, max_backoff=timedelta(hours=settings.source_max_backoff_hours)
        )
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


BATCH_TIMEOUT = 900  # пачка конвейера обычно идёт секунды


class BatchResult(Protocol):
    @property
    def total(self) -> int: ...


async def drain_loop(
    name: str, process_batch: Callable[[], Awaitable[BatchResult]], tick: float, stop: asyncio.Event
) -> None:
    """Обрабатывать пачки подряд, пока есть работа, потом ждать tick секунд."""
    while not stop.is_set():
        try:
            # Зависшая пачка не должна остановить шаг навсегда: отменить и начать заново
            async with asyncio.timeout(BATCH_TIMEOUT):
                result = await process_batch()
        except TimeoutError:
            logger.error("шаг «%s»: пачка не закончилась за %d мин, повторяю", name, BATCH_TIMEOUT // 60)
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


HEARTBEAT_SECONDS = 30
RETENTION_SECONDS = 3600
RETENTION_BATCH = 1000


async def heartbeat_loop(stop: asyncio.Event) -> None:
    """«Пульс»: сводка и /health видят, что worker жив."""
    started_at = datetime.now(UTC)
    while not stop.is_set():
        try:
            async with async_session() as session, session.begin():
                await PipelineRepository(session).beat("worker", started_at)
        except Exception:
            logger.exception("не удалось записать пульс")
        await sleep_until_stop(stop, HEARTBEAT_SECONDS)


async def retention_loop(stop: asyncio.Event) -> None:
    """Раз в час убирать тексты и векторы старых постов (ТЗ: храним 90 дней)."""
    while not stop.is_set():
        before = datetime.now(UTC) - timedelta(days=settings.post_retention_days)
        try:
            total = 0
            while not stop.is_set():
                async with async_session() as session, session.begin():
                    purged = await PipelineRepository(session).purge_old_texts(before, RETENTION_BATCH)
                total += purged
                if purged < RETENTION_BATCH:
                    break
            if total:
                logger.info("хранение: убраны тексты и векторы %d старых постов", total, extra={"purged": total})
            async with async_session() as session, session.begin():
                if removed := await PipelineRepository(session).purge_service_rows(datetime.now(UTC)):
                    logger.info("хранение: удалено служебных записей: %d", removed)
        except Exception:
            logger.exception("ошибка чистки старых постов")
        try:
            # Издания источников (ТАСС из RSS и из Telegram — одно) и «N изданий» у свежих сюжетов
            async with async_session() as session, session.begin():
                changed = await SourceRepository(session).detect_outlets()
                if changed:
                    recounted = await StoryRepository(session).recount_outlets()
                    logger.info("издания: определены у %d источников, пересчитано сюжетов: %d", changed, recounted)
        except Exception:
            logger.exception("ошибка определения изданий")
        await sleep_until_stop(stop, RETENTION_SECONDS)


ALERT_SECONDS = 300
DIGEST_SECONDS = 60


CHECK_SECONDS = 120


async def topic_check_loop(stop: asyncio.Event) -> None:
    """Раз в 2 минуты: LLM проверяет новые совпадения тем, где включена проверка по описанию."""
    client = LlmClient.from_settings(settings)
    if client is None:
        return
    service = TopicCheckService(async_session, client, settings)
    while not stop.is_set():
        try:
            checked, rejected = await service.run_once()
            if checked:
                logger.info("проверка тем: проверено %d, убрано %d", checked, rejected)
        except Exception:
            logger.exception("ошибка проверки тем")
        await sleep_until_stop(stop, CHECK_SECONDS)


MERGE_SECONDS = 180


async def merge_loop(stop: asyncio.Event) -> None:
    """Раз в 3 минуты: LLM решает, не одно ли событие у нового сюжета и очень похожего сюжета рядом."""
    client = LlmClient.from_settings(settings)
    if client is None or not settings.cluster_merge_enabled:
        return
    service = StoryMergeService(async_session, client, settings)
    while not stop.is_set():
        try:
            asked, merged = await service.run_once()
            if asked:
                logger.info("объединение сюжетов: спросили про %d пар, объединили %d", asked, merged)
        except Exception:
            logger.exception("ошибка объединения сюжетов")
        await sleep_until_stop(stop, MERGE_SECONDS)


async def digest_loop(stop: asyncio.Event) -> None:
    """Раз в минуту: дайджесты тем, у которых подошло время по расписанию (если LLM настроена)."""
    client = LlmClient.from_settings(settings)
    if client is None:
        return
    service = DigestService(async_session, client, settings)
    while not stop.is_set():
        try:
            made = await service.run_due()
            if made:
                logger.info("дайджестов сделано: %d", made)
        except Exception:
            logger.exception("ошибка дайджестов")
        await sleep_until_stop(stop, DIGEST_SECONDS)


async def alerts_loop(stop: asyncio.Event) -> None:
    """Раз в 5 минут: если что-то сломалось или починилось — сообщение в «Избранное» служебного Telegram.

    Отправляет подключение Telegram (поручение notify). Какие проблемы уже сообщены, видно по прошлому
    поручению: после перезапуска worker'а уведомления не повторяются.
    """
    while not stop.is_set():
        try:
            async with async_session() as session, session.begin():
                repo = ConnectorRepository(session)
                target = next((c for c in await repo.list_enabled([SourceType.TELEGRAM])
                               if c.status == ConnectorStatus.ACTIVE and c.settings.get("alerts", True)), None)
                if target is not None:
                    problems = {p.key: p for p in await find_problems(session, datetime.now(UTC))}
                    last = await repo.last_command(target.id, "notify")
                    known: dict[str, str] = dict(last.payload.get("problems", {})) if last else {}
                    appeared = [p for key, p in problems.items() if key not in known]
                    fixed = [Problem(key, text) for key, text in known.items() if key not in problems]
                    if appeared or fixed:
                        await repo.add_command(target.id, "notify", {
                            "text": alert_text(appeared, fixed),
                            "problems": {key: p.text for key, p in problems.items()},
                        })
        except Exception:
            logger.exception("ошибка уведомлений")
        await sleep_until_stop(stop, ALERT_SECONDS)


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
            async_session, ClusteringRules.from_settings(settings), batch_size=settings.cluster_batch_size
        )
        matching = TopicMatchingService(async_session, TopicRules.from_settings(settings))
        box = SecretBox(settings.secret_key.get_secret_value()) if settings.secret_key else None
        supervisor = ConnectorSupervisor(async_session, LISTENERS, ingest, box)
        logger.info("worker запущен")

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(poll_loop(ingest, stop))
            tasks.create_task(drain_loop("очистка", cleaning.process_batch, settings.clean_tick_seconds, stop))
            # Леммы постов, собранных до поиска по леммам; когда их нет, цикл просто ждёт
            tasks.create_task(embed_loop(stop))
            tasks.create_task(
                drain_loop("склейка", clustering.process_batch, settings.cluster_tick_seconds, stop)
            )
            tasks.create_task(
                drain_loop("темы", matching.process_batch, settings.topic_match_tick_seconds, stop)
            )
            tasks.create_task(supervisor.run(stop, settings.connector_check_seconds))
            tasks.create_task(heartbeat_loop(stop))
            tasks.create_task(retention_loop(stop))
            tasks.create_task(alerts_loop(stop))
            tasks.create_task(digest_loop(stop))
            tasks.create_task(topic_check_loop(stop))
            tasks.create_task(merge_loop(stop))
            if settings.full_text_enabled:
                full_text = FullTextService(
                    async_session, client, concurrency=settings.full_text_concurrency,
                    short_post_chars=settings.full_text_short_post_chars,
                )
                tasks.create_task(drain_loop("полный текст", full_text.process_batch, 30, stop))

    await engine.dispose()
    logger.info("worker остановлен")


def run() -> None:
    setup_logging(settings.log_level, settings.log_format, settings.log_sql)
    asyncio.run(main())


if __name__ == "__main__":
    run()
