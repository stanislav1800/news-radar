from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin, str_enum
from sito.models.enums import SourceType


class Source(IdMixin, TimestampMixin, Base):
    __tablename__ = "source"
    __table_args__ = (
        CheckConstraint("poll_interval > 0", name="poll_interval_positive"),
    )

    type: Mapped[SourceType] = mapped_column(str_enum(SourceType, "source_type"))
    name: Mapped[str]
    url: Mapped[str] = mapped_column(unique=True)
    enabled: Mapped[bool] = mapped_column(server_default="true")
    poll_interval: Mapped[int] = mapped_column(server_default="300")
    
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    last_error_at: Mapped[datetime | None]
