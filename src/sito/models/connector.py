from typing import Any

from sqlalchemy import Index, LargeBinary, text
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
