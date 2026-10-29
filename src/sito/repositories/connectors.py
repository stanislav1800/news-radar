from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Connector, SourceType


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
