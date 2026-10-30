"""/health: для мониторинга и проверки контейнера. Без входа, без подробностей о данных."""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sito.repositories.pipeline import PipelineRepository

logger = logging.getLogger(__name__)
router = APIRouter()

# Worker отмечается раз в 30 секунд: четыре пропуска подряд — он не работает
WORKER_STALE_AFTER = timedelta(minutes=2)


def worker_state(beat_at: datetime | None, now: datetime) -> str:
    if beat_at is None:
        return "unknown"  # ни разу не запускался с этой базой
    return "ok" if now - beat_at <= WORKER_STALE_AFTER else "stale"


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """200 — веб и база работают; 503 — база недоступна. Состояние worker'а — для информации:
    веб без worker'а работает, просто новые посты не появляются."""
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
            beat = await PipelineRepository(session).heartbeat("worker")
    except (SQLAlchemyError, OSError) as exc:  # база не отвечает или отказала в подключении
        logger.warning("health: база недоступна: %s", exc)
        return JSONResponse({"status": "error", "database": "error"}, status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({
        "status": "ok",
        "database": "ok",
        "worker": worker_state(beat.beat_at if beat else None, datetime.now(UTC)),
    })
