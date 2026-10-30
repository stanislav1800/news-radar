from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import SourceType
from sito.repositories.sources import SourceRepository, silence_limit
from tests.conftest import MakeSource

POLLED = (SourceType.RSS,)
LONG_AGO = func.now() - timedelta(hours=1)
JUST_NOW = func.now() - timedelta(seconds=10)


async def test_due_for_polling(session: AsyncSession, make_source: MakeSource) -> None:
    never_polled = await make_source()
    polled_long_ago = await make_source(last_success_at=LONG_AGO)
    await make_source(last_success_at=JUST_NOW)                       # интервал ещё не прошёл
    await make_source(last_error_at=JUST_NOW, last_error="boom")      # упал только что: не долбим
    failed_long_ago = await make_source(last_error_at=LONG_AGO, last_error="boom")
    await make_source(enabled=False)                                  # выключен
    await make_source(type=SourceType.TELEGRAM)                       # не опрашивается по расписанию

    due = await SourceRepository(session).due_for_polling(POLLED)

    # Сначала никогда не опрошенные, потом ждущие дольше всех
    assert due[0] == never_polled
    assert set(due) == {never_polled, polled_long_ago, failed_long_ago}


async def test_recent_error_after_old_success_is_not_due(session: AsyncSession, make_source: MakeSource) -> None:
    # Учитывается последняя попытка, а не последний успех
    await make_source(last_success_at=LONG_AGO, last_error_at=JUST_NOW, last_error="boom")

    assert await SourceRepository(session).due_for_polling(POLLED) == []


async def test_mark_success_clears_error(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source(last_error="boom", last_error_at=JUST_NOW)
    repo = SourceRepository(session)

    await repo.mark_success(source_id, {"etag": '"v1"'})

    source = await repo.get(source_id)
    assert source is not None
    await session.refresh(source)
    assert source.state == {"etag": '"v1"'}
    assert source.last_success_at is not None
    assert source.last_error is None


def ago(minutes: int) -> object:
    return func.now() - timedelta(minutes=minutes)


async def test_failures_make_polling_rarer(session: AsyncSession, make_source: MakeSource) -> None:
    """Пауза удваивается с каждой неудачей подряд: 5, 10, 20, 40 минут… но не больше max_backoff."""
    second_failure_due = await make_source(failures=2, last_error_at=ago(11), last_error="boom")  # пауза 10 мин
    await make_source(failures=3, last_error_at=ago(19), last_error="boom")                        # пауза 20 мин
    capped = await make_source(failures=20, last_error_at=ago(61), last_error="boom")              # потолок — час

    due = await SourceRepository(session).due_for_polling(POLLED, max_backoff=timedelta(hours=1))

    assert set(due) == {second_failure_due, capped}


async def test_failure_counter(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source()
    repo = SourceRepository(session)

    await repo.mark_failed(source_id, "HTTP 429")
    await repo.mark_failed(source_id, "HTTP 429")
    source = await repo.get(source_id)
    assert source is not None
    await session.refresh(source)
    assert source.failures == 2

    await repo.mark_success(source_id)
    await session.refresh(source)
    assert source.failures == 0 and source.last_error is None


def test_silence_limit_depends_on_how_often_source_writes() -> None:
    assert silence_limit(posts_week=7000) == timedelta(hours=6)   # ТАСС: раз в полторы минуты → 6 часов
    assert silence_limit(posts_week=14) == timedelta(days=3)      # OpenNET: два раза в день → 3 суток
    assert silence_limit(posts_week=140) == timedelta(hours=12)   # 20 в день → 10 × 1,2 часа
