from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin, str_enum
from sito.models.enums import PostStatus

# Время новости: когда опубликовал источник, но не позже, чем мы её скачали.
# Некоторые ленты путают часовой пояс и ставят время из будущего. LEAST в Postgres пропускает NULL.
EVENT_AT_SQL = "least(post.published_at, post.fetched_at)"

# Размер вектора задаёт модель эмбеддингов. Смена модели с другим размером — миграция и пересчёт.
EMBEDDING_DIM = 768


class Post(IdMixin, TimestampMixin, Base):
    __tablename__ = "post"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        Index(
            "ix_posts_status_pending", "status", "id",
            postgresql_where="status NOT IN ('clustered', 'skipped', 'failed')",
        ),
        # Время новости (см. EVENT_AT): склейка ищет соседей в окне по нему
        Index("ix_post_event_at", text("least(published_at, fetched_at)")),
    )
    
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("source.id", ondelete="CASCADE"))
    external_id: Mapped[str]
    
    url: Mapped[str]
    url_normalized: Mapped[str] = mapped_column(unique=True)
    
    title: Mapped[str]
    raw_text: Mapped[str | None]
    text: Mapped[str | None]       # очищенный, шаг 3
    links: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")  # ссылки из текста, шаг 3
    content_hash: Mapped[str]

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))  # шаг 4
    embedding_model: Mapped[str | None]  # какой моделью посчитан: при смене модели видно, что пересчитать

    published_at: Mapped[datetime | None] = mapped_column(index=True)
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())

    story_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("story.id", ondelete="SET NULL"), index=True
    )  # шаг 5

    status: Mapped[PostStatus] = mapped_column(str_enum(PostStatus, "post_status"), server_default=PostStatus.NEW.value)
    attempt: Mapped[int] = mapped_column(server_default="0")
    last_error: Mapped[str | None]
