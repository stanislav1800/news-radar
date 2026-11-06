from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin, str_enum
from sito.models.enums import ConnectorStatus, SourceType


class Connector(IdMixin, TimestampMixin, Base):
    """Подключение: тип источников со своими настройками и учётными данными.

    Источники живут внутри подключения. RSS — встроенное подключение без учётных данных,
    дальше Telegram (аккаунт), VK (токен). Подключений одного типа может быть несколько.
    """

    __tablename__ = "connector"
    __table_args__ = (
        # Встроенное подключение каждого типа одно (сейчас это RSS)
        Index("uq_connector_builtin_type", "type", unique=True, postgresql_where=text("builtin")),
    )

    type: Mapped[SourceType] = mapped_column(str_enum(SourceType, "connector_type"))
    name: Mapped[str]
    builtin: Mapped[bool] = mapped_column(server_default="false")  # встроенное нельзя удалить
    enabled: Mapped[bool] = mapped_column(server_default="true")
    # Настройки без секретов: их можно показывать в админке
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    # Секреты (сессия Telegram, токены): JSON, зашифрованный ключом из SITO_SECRET_KEY
    # (sito.utils.crypto). Без ключа дамп базы их не раскрывает. В вебе не показываются.
    secrets: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[ConnectorStatus] = mapped_column(
        str_enum(ConnectorStatus, "connector_status"), server_default=ConnectorStatus.ACTIVE.value
    )
    last_error: Mapped[str | None]


class ConnectorCommand(IdMixin, Base):
    """Поручение подключению из веба: показать канал по ссылке, отписаться.

    Сессией Telegram пользуется только worker, поэтому веб не ходит в Telegram сам, а оставляет
    команду; worker выполняет её за пару секунд и пишет результат, страница ждёт его.
    """

    __tablename__ = "connector_command"
    __table_args__ = (Index("ix_connector_command_pending", "connector_id", postgresql_where=text("status = 'pending'")),)

    connector_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("connector.id", ondelete="CASCADE"))
    kind: Mapped[str]                       # preview, leave
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(server_default="pending")  # pending, running, done, failed
    result: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
