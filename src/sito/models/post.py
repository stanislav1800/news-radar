from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin, str_enum
from sito.models.enums import PostStatus


class Post(IdMixin, TimestampMixin, Base):
    __tablename__ = "post"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        Index(
            "ix_posts_status_pending", "status", "id",
            postgresql_where="status NOT IN ('clustered', 'skipped', 'failed')",
        ),
    )
    
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("source.id", ondelete="CASCADE"))
    external_id: Mapped[str]
    
    url: Mapped[str]
    url_normalized: Mapped[str] = mapped_column(unique=True)
    
    title: Mapped[str]
    raw_text: Mapped[str | None]
    text: Mapped[str | None]
    content_hash: Mapped[str]

    published_at: Mapped[datetime | None] = mapped_column(index=True)
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())

    status: Mapped[PostStatus] = mapped_column(str_enum(PostStatus, "post_status"), server_default=PostStatus.NEW.value)
    attempt: Mapped[int] = mapped_column(server_default="0")
    last_error: Mapped[str | None]
