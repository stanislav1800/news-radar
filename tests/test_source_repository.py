from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import SourceType
from sito.repositories.sources import SourceRepository
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

    await repo.mark_success(source_id, etag='"v1"', last_modified=None)

    source = await repo.get(source_id)
    assert source is not None
    await session.refresh(source)
    assert source.etag == '"v1"'
    assert source.last_success_at is not None
    assert source.last_error is None
