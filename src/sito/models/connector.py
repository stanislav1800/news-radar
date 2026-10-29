from typing import Any

from sqlalchemy import Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin, str_enum
from sito.models.enums import SourceType


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
    # Настройки без секретов. Секреты (сессия Telegram, токены) появятся вместе с Telegram,
    # в отдельной зашифрованной колонке.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    last_error: Mapped[str | None]
