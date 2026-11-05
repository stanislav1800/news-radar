from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey
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
    # Пояс ещё не определён: при первом заходе возьмётся из браузера (один раз)
    timezone_auto: Mapped[bool] = mapped_column(server_default="true")
    last_login_at: Mapped[datetime | None]

    # «Новое с прошлого визита». Визит — заходы без перерыва дольше 30 минут.
    last_seen_at: Mapped[datetime | None]       # последний заход в ленту
    previous_visit_at: Mapped[datetime | None]  # последний заход в прошлый визит: граница «нового»
    # «Отметить всё прочитанным»: сюжеты, не менявшиеся после этого момента, считаются прочитанными
    read_all_before: Mapped[datetime | None]
    # Какие общие вкладки показывать: кто живёт только своими темами, может убрать «Главное» и «Все новости»
    show_top: Mapped[bool] = mapped_column(server_default="true")
    show_all: Mapped[bool] = mapped_column(server_default="true")


class UserHiddenSource(Base):
    """Источник, который пользователь не хочет видеть в «Главном» и «Всех новостях» (темы — сами по себе)."""

    __tablename__ = "user_hidden_source"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user_account.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("source.id", ondelete="CASCADE"), primary_key=True)
