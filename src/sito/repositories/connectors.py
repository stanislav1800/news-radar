from collections.abc import Collection
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Connector, ConnectorCommand, ConnectorStatus, Source, SourceType

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

    # --- Команды из веба (выполняет worker) ---------------------------------------------------------

    async def add_command(self, connector_id: int, kind: str, payload: dict[str, Any]) -> ConnectorCommand:
        command = ConnectorCommand(connector_id=connector_id, kind=kind, payload=payload)
        self.session.add(command)
        await self.session.flush()
        return command

    async def get_command(self, command_id: int) -> ConnectorCommand | None:
        return await self.session.get(ConnectorCommand, command_id)

    async def take_command(self, connector_id: int) -> ConnectorCommand | None:
        """Взять следующее поручение в работу (status → running): если результат потом не запишется,
        поручение не выполнится второй раз — отписка или уведомление не повторятся."""
        command = await self.session.scalar(
            select(ConnectorCommand)
            .where(ConnectorCommand.connector_id == connector_id, ConnectorCommand.status == "pending")
            .order_by(ConnectorCommand.id).limit(1).with_for_update(skip_locked=True)
        )
        if command is not None:
            command.status = "running"
        return command

    async def fail_interrupted(self, connector_id: int) -> None:
        """Поручения, которые выполнялись, когда соединение или worker упали: веб не должен ждать их вечно."""
        await self.session.execute(
            update(ConnectorCommand)
            .where(ConnectorCommand.connector_id == connector_id, ConnectorCommand.status == "running")
            .values(status="failed", error="Выполнение прервалось — попробуйте ещё раз", finished_at=func.now())
        )

    async def last_command(self, connector_id: int, kind: str) -> ConnectorCommand | None:
        return await self.session.scalar(
            select(ConnectorCommand).where(ConnectorCommand.connector_id == connector_id, ConnectorCommand.kind == kind)
            .order_by(ConnectorCommand.id.desc()).limit(1)
        )

    async def finish_command(self, command_id: int, result: Any = None, error: str | None = None) -> None:
        await self.session.execute(
            update(ConnectorCommand).where(ConnectorCommand.id == command_id).values(
                status="failed" if error else "done", result=result,
                error=error[:ERROR_MAX_LENGTH] if error else None, finished_at=func.now(),
            )
        )
