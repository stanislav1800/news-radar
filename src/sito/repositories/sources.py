from collections.abc import Collection

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Source, SourceType

ERROR_MAX_LENGTH = 2000


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, source_id: int) -> Source | None:
        return await self.session.get(Source, source_id)

    async def due_for_polling(self, types: Collection[SourceType], limit: int = 100) -> list[int]:
        """Включённые источники, у которых с последней попытки прошло poll_interval секунд.

        Попытка — это и успех, и ошибка: упавший сайт опрашивается с тем же интервалом,
        а не на каждом круге цикла.
        """
        last_attempt = func.greatest(Source.last_success_at, Source.last_error_at)  # NULL не учитывается
        next_poll = last_attempt + func.make_interval(0, 0, 0, 0, 0, 0, Source.poll_interval)
        result = await self.session.execute(
            select(Source.id)
            .where(
                Source.enabled,
                Source.type.in_(types),
                or_(last_attempt.is_(None), next_poll <= func.now()),
            )
            .order_by(last_attempt.asc().nulls_first())  # сначала новые и самые давние
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_success(self, source_id: int, etag: str | None, last_modified: str | None) -> None:
        await self.session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(
                etag=etag,
                last_modified=last_modified,
                last_success_at=func.now(),
                last_error=None,
                last_error_at=None,
            )
        )

    async def mark_failed(self, source_id: int, error: str) -> None:
        await self.session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(last_error=error[:ERROR_MAX_LENGTH], last_error_at=func.now())
        )
