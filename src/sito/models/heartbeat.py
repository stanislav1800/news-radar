from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base


class Heartbeat(Base):
    """«Пульс» фонового процесса: worker отмечается раз в полминуты, сводка и /health видят, жив ли он."""

    __tablename__ = "heartbeat"

    name: Mapped[str] = mapped_column(primary_key=True)  # worker
    started_at: Mapped[datetime]
    beat_at: Mapped[datetime] = mapped_column(server_default=func.now())
