from collections.abc import Collection

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Connector, ConnectorStatus, Source, SourceType

ERROR_MAX_LENGTH = 2000


class ConnectorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, connector_id: int) -> Connector | None:
        return await self.session.get(Connector, connector_id)

    async def get_builtin(self, type_: SourceType) -> Connector | None:
        return await self.session.scalar(select(Connector).where(Connector.builtin, Connector.type == type_))

    async def list_all(self) -> list[Connector]:
        # Встроенные первыми
        return list(await self.session.scalars(select(Connector).order_by(Connector.builtin.desc(), Connector.id)))

    async def list_enabled(self, types: Collection[SourceType]) -> list[Connector]:
        return list(await self.session.scalars(
            select(Connector).where(Connector.enabled, Connector.type.in_(types)).order_by(Connector.id)
        ))

    async def has_sources(self, connector_id: int) -> bool:
        return await self.session.scalar(select(Source.id).where(Source.connector_id == connector_id).limit(1)) is not None

    async def set_status(self, connector_id: int, status: ConnectorStatus, error: str | None = None) -> None:
        """Статус подключения; error — что случилось (при ACTIVE ошибка снимается)."""
        await self.session.execute(
            update(Connector).where(Connector.id == connector_id)
            .values(status=status, last_error=error[:ERROR_MAX_LENGTH] if error else None)
        )
