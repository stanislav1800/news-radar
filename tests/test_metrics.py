import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito import metrics
from sito.models import PostStatus
from sito.services.llm import LlmAnswer, record_usage
from sito.worker.__main__ import drain_loop
from tests.conftest import MakeSource
from tests.test_pipeline import add_post


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_state_from_database(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    now = datetime.now(UTC)
    source = await make_source(name="ТАСС")
    await add_post(session_factory, source, 1, PostStatus.NEW, fetched_at=now - timedelta(minutes=10))
    # Опубликован 5 минут назад, собран через 2, в ленте ещё через минуту
    await add_post(
        session_factory, source, 2, PostStatus.CLUSTERED,
        published_at=now - timedelta(minutes=5), fetched_at=now - timedelta(minutes=3),
        clustered_at=now - timedelta(minutes=2),
    )

    async with session_factory() as session:
        await metrics.refresh_state(session, now)

    assert sample("sito_posts", status="new") == 1
    assert sample("sito_posts", status="clustered") == 1
    assert sample("sito_pipeline_oldest_pending_seconds") == pytest.approx(600, abs=5)
    assert sample("sito_post_lag_posts", source_type="rss") == 1
    assert sample("sito_post_lag_seconds", source_type="rss", stage="collect", quantile="0.5") == pytest.approx(120)
    assert sample("sito_post_lag_seconds", source_type="rss", stage="process", quantile="0.9") == pytest.approx(60)
    assert sample("sito_sources", source_type="rss", state="ok") == 1


async def test_pipeline_step_batches_and_errors() -> None:
    stop = asyncio.Event()
    calls = 0

    class Result:
        def __init__(self, total: int) -> None:
            self.total = total

    async def batch() -> Result:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("сломалось")
        if calls == 2:
            return Result(3)
        stop.set()
        return Result(0)

    items = sample("sito_batch_items_total", step="тест")
    errors = sample("sito_step_errors_total", step="тест", kind="error")
    await drain_loop("тест", batch, 0, stop)

    assert sample("sito_batch_items_total", step="тест") == items + 3
    assert sample("sito_step_errors_total", step="тест", kind="error") == errors + 1
    assert sample("sito_batch_duration_seconds_count", step="тест") >= 1


async def test_llm_usage(session: AsyncSession) -> None:
    before = sample("sito_llm_cost_rub_total", feature="тест")
    record_usage(session, "тест", LlmAnswer("ok", "m", 100, 20, 0.5, 3.0), None)

    assert sample("sito_llm_cost_rub_total", feature="тест") == before + 0.5
    assert sample("sito_llm_tokens_total", feature="тест", direction="output") >= 20


async def test_http_requests_by_route_template(client: httpx.AsyncClient) -> None:
    before = sample("sito_http_requests_total", method="GET", route="/health", status="200")
    assert (await client.get("/health")).status_code == 200
    assert sample("sito_http_requests_total", method="GET", route="/health", status="200") == before + 1
