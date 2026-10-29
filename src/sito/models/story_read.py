from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base


class StoryRead(Base):
    """Пользователь открыл сюжет. Если после read_at в сюжете появились посты — в нём есть обновления."""

    __tablename__ = "story_read"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_account.id", ondelete="CASCADE"), primary_key=True
    )
    story_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("story.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    read_at: Mapped[datetime] = mapped_column(server_default=func.now())
