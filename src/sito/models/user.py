from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from sito.db.base import Base, IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    # «user» в PostgreSQL зарезервированное слово
    __tablename__ = "user_account"

    username: Mapped[str] = mapped_column(unique=True)  # в нижнем регистре
    password_hash: Mapped[str]
    is_admin: Mapped[bool] = mapped_column(server_default="false")
    is_active: Mapped[bool] = mapped_column(server_default="true")
    # Входит в cookie сессии. Смена пароля увеличивает версию, и все прежние входы перестают действовать.
    session_version: Mapped[int] = mapped_column(server_default="1")
    timezone: Mapped[str] = mapped_column(server_default="Europe/Moscow")
    last_login_at: Mapped[datetime | None]
