"""Метрики для Prometheus.

Два вида:
- события (счётчики и гистограммы) — растут там, где что-то произошло: пачка конвейера, сохранённые посты,
  вызов LLM, HTTP-запрос. У каждого процесса свои, Prometheus складывает;
- состояние (gauge) — снимок из базы раз в минуту: очередь конвейера, задержка до ленты, источники, расход LLM.
  Снимает только worker (refresh_state), чтобы не считать одно и то же в двух процессах.

Метрики отдаются на отдельном порту (METRICS_PORT), а не на сайте: порт не публикуется наружу,
его видит только Prometheus внутри docker-сети.
"""

import logging
from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Connector, ConnectorStatus, LlmUsage, Post
from sito.repositories.pipeline import IN_PROGRESS
from sito.repositories.sources import SourceRepository

logger = logging.getLogger(__name__)

# --- события ---------------------------------------------------------------------------------

BATCH_SECONDS = Histogram(
    "sito_batch_duration_seconds", "Сколько шла пачка шага конвейера (только непустые)", ["step"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900),
)
BATCH_ITEMS = Counter("sito_batch_items_total", "Сколько записей обработали шаги конвейера", ["step"])
STEP_ERRORS = Counter("sito_step_errors_total", "Пачки, упавшие с ошибкой или по таймауту", ["step", "kind"])

POSTS_SAVED = Counter(
    "sito_posts_saved_total", "Итог сохранения собранных постов", ["result"],  # inserted, updated, duplicate…
)
POLL_ERRORS = Counter("sito_source_poll_errors_total", "Ошибки опроса RSS-источников", ["error"])
CONNECTOR_RESTARTS = Counter(
    "sito_connector_restarts_total", "Перезапуски подключений (Telegram)", ["reason"],  # silence, failure
)

LLM_CALLS = Counter("sito_llm_calls_total", "Вызовы LLM", ["feature"])
LLM_TOKENS = Counter("sito_llm_tokens_total", "Токены LLM", ["feature", "direction"])
LLM_COST = Counter("sito_llm_cost_rub_total", "Стоимость вызовов LLM по цене сервиса, ₽", ["feature"])
LLM_SECONDS = Histogram(
    "sito_llm_call_duration_seconds", "Сколько отвечала модель", ["feature"],
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120),
)

HTTP_REQUESTS = Counter("sito_http_requests_total", "Запросы к сайту", ["method", "route", "status"])
HTTP_SECONDS = Histogram(
    "sito_http_request_duration_seconds", "Время ответа сайта", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- состояние из базы -----------------------------------------------------------------------

POSTS_BY_STATUS = Gauge("sito_posts", "Посты по статусу конвейера", ["status"])
OLDEST_PENDING = Gauge(
    "sito_pipeline_oldest_pending_seconds", "Сколько ждёт самый старый пост, не дошедший до ленты (0 — очереди нет)",
)
LAG = Gauge(
    "sito_post_lag_seconds",
    "Задержка постов, попавших в ленту за последние LAG_WINDOW: stage=collect — от публикации до сбора, "
    "process — от сбора до ленты",
    ["source_type", "stage", "quantile"],
)
LAG_POSTS = Gauge("sito_post_lag_posts", "Сколько постов попало в ленту за LAG_WINDOW", ["source_type"])
SOURCES = Gauge("sito_sources", "Включённые источники по состоянию", ["source_type", "state"])  # ok, failing, silent
CONNECTOR_UP = Gauge("sito_connector_up", "Подключение работает (1) или нет (0)", ["connector"])
LLM_COST_24H = Gauge("sito_llm_cost_rub_24h", "Расход на LLM за последние сутки, ₽")

LAG_WINDOW = timedelta(minutes=15)

# Как _LAG_SQL у админки, но за короткое окно: график должен реагировать за минуты, а не за сутки
_LAG_SQL = text("""
    SELECT source.type AS source_type, count(*) AS posts,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY greatest(0, extract(epoch FROM post.fetched_at - post.published_at)))
               AS collect_p50,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY greatest(0, extract(epoch FROM post.fetched_at - post.published_at)))
               AS collect_p90,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM post.clustered_at - post.fetched_at))
               AS process_p50,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM post.clustered_at - post.fetched_at))
               AS process_p90
    FROM post JOIN source ON source.id = post.source_id
    WHERE post.clustered_at > now() - make_interval(secs => :window) AND post.published_at IS NOT NULL
    GROUP BY source.type
""")

_started = False


def serve(port: int | None) -> None:
    """Отдавать метрики на порту (в отдельном потоке). None — метрики не нужны (тесты, разработка)."""
    global _started
    if port is None or _started:
        return
    start_http_server(port)
    _started = True
    logger.info("метрики Prometheus: порт %d", port)


async def refresh_state(session: AsyncSession, now: datetime | None = None) -> None:
    """Снять состояние из базы в gauge-метрики."""
    now = now or datetime.now(UTC)

    rows = await session.execute(select(Post.status, func.count()).group_by(Post.status))
    by_status = {status: count for status, count in rows}
    POSTS_BY_STATUS.clear()
    for status, count in by_status.items():
        POSTS_BY_STATUS.labels(str(status)).set(count)
    oldest = await session.scalar(select(func.min(Post.fetched_at)).where(Post.status.in_(IN_PROGRESS)))
    OLDEST_PENDING.set((now - oldest).total_seconds() if oldest else 0)

    LAG.clear()
    LAG_POSTS.clear()
    for row in await session.execute(_LAG_SQL, {"window": LAG_WINDOW.total_seconds()}):
        m = row._mapping
        LAG_POSTS.labels(m["source_type"]).set(m["posts"])
        for stage in ("collect", "process"):
            for q, quantile in (("p50", "0.5"), ("p90", "0.9")):
                if (value := m[f"{stage}_{q}"]) is not None:
                    LAG.labels(m["source_type"], stage, quantile).set(value)

    counts: dict[tuple[str, str], int] = {}
    for s in await SourceRepository(session).list_with_stats():
        if not s.enabled:
            continue
        state = "failing" if s.failing else "silent" if s.silent_since(now) else "ok"
        counts[s.type, state] = counts.get((s.type, state), 0) + 1
    SOURCES.clear()
    for (source_type, state), count in counts.items():
        SOURCES.labels(source_type, state).set(count)

    CONNECTOR_UP.clear()
    for c in (await session.scalars(select(Connector).where(Connector.enabled, ~Connector.builtin))).all():
        CONNECTOR_UP.labels(c.name).set(1 if c.status == ConnectorStatus.ACTIVE else 0)

    cost = await session.scalar(select(func.sum(LlmUsage.cost)).where(LlmUsage.created_at >= now - timedelta(days=1)))
    LLM_COST_24H.set(float(cost or 0))
