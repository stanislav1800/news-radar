from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin, str_enum
from sito.models.enums import SourceType


class Source(IdMixin, TimestampMixin, Base):
    """Источник постов внутри подключения: RSS-лента, Telegram-канал.

    Как источник опознаётся, зависит от типа: у RSS это адрес ленты (url), у Telegram — id канала
    (external_id). Источники опрашиваемых типов (RSS) добавляет администратор, источники
    «слушающих» (Telegram) заводятся сами с первым постом.
    """

    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("type", "external_id"),
        CheckConstraint("poll_interval IS NULL OR poll_interval > 0", name="poll_interval_positive"),
        CheckConstraint("url IS NOT NULL OR external_id IS NOT NULL", name="has_identity"),
    )

    # RESTRICT: подключение с источниками удалить нельзя, иначе вместе с ними пропали бы все посты
    connector_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("connector.id", ondelete="RESTRICT"), index=True
    )
    type: Mapped[SourceType] = mapped_column(str_enum(SourceType, "source_type"))  # совпадает с типом подключения
    name: Mapped[str]
    url: Mapped[str | None] = mapped_column(unique=True)  # адрес ленты (RSS) или страницы источника
    external_id: Mapped[str | None]                       # id у сервиса: канал Telegram
    enabled: Mapped[bool] = mapped_column(server_default="true")
    # Издание (домен сайта: tass.ru). ТАСС из RSS и Telegram-канал ТАСС — одно издание, в сюжете это
    # «1 издание», а не «2». Определяет worker по ссылкам постов (SourceRepository.detect_outlets)
    outlet: Mapped[str | None]
    poll_interval: Mapped[int | None]  # секунд; только у опрашиваемых типов

    # Курсор сборщика, формат знает только он: у RSS — ETag и Last-Modified,
    # у Telegram — номер последнего сохранённого сообщения (догрузка после простоя)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    # Что сервис рассказывает об источнике, для админки: у Telegram-канала — подписчики, описание,
    # отписан ли аккаунт, приглушён ли канал на телефоне. Формат знает сборщик
    info: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")

    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    last_error_at: Mapped[datetime | None]
    # Неудач подряд: по ним растёт пауза перед следующим опросом. Успех обнуляет
    failures: Mapped[int] = mapped_column(server_default="0")
