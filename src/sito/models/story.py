from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin
from sito.models.post import EMBEDDING_DIM


class Story(IdMixin, TimestampMixin, Base):
    """Сюжет: одно событие, которое собирает посты из разных источников (шаг 5).

    Все поля, кроме id, — сводка по постам сюжета. Её пересчитывает StoryRepository.refresh.
    """

    __tablename__ = "story"

    title: Mapped[str]             # из самого свежего поста: в нём обычно последние данные
    lead: Mapped[str | None]       # анонс свежего поста, не длиннее ~300 знаков (ТЗ)
    posts_count: Mapped[int] = mapped_column(server_default="0")
    sources_count: Mapped[int] = mapped_column(server_default="0")  # разных изданий, а не постов

    # Кто написал первым
    first_post_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("post.id", ondelete="SET NULL", use_alter=True)
    )
    first_source_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("source.id", ondelete="SET NULL"))

    started_at: Mapped[datetime]                                   # время первого поста
    last_post_at: Mapped[datetime] = mapped_column(index=True)    # время самого свежего поста

    # Средний вектор постов: для поиска похожих сюжетов
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
