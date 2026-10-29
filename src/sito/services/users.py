"""Пользователи: создание, проверка пароля при входе, смена пароля. Общее для веба и CLI."""

import asyncio
import re
from datetime import UTC, datetime
from functools import cache

from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import User
from sito.repositories.users import UserRepository

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
MIN_PASSWORD_LENGTH = 8

# argon2: медленный нарочно (около 0,1 с), это и защищает от подбора по украденной базе
_hasher = PasswordHash.recommended()


class UserError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


class UserCreateForm(BaseModel):
    """Форма «Новый пользователь» на странице администратора."""

    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(title="Логин", description="Латиница, цифры, точка, дефис, подчёркивание", min_length=1)
    password: SecretStr = Field(title="Пароль", description=f"Не короче {MIN_PASSWORD_LENGTH} символов", min_length=1)
    is_admin: bool = Field(False, title="Администратор")


def normalize_username(raw: str) -> str:
    username = raw.strip().lower()
    if not USERNAME_RE.match(username):
        raise UserError("Логин: от 3 до 32 символов, латиница, цифры, точка, дефис и подчёркивание")
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")


async def hash_password(password: str) -> str:
    # В отдельном потоке: argon2 занимает процессор и иначе остановил бы обработку других запросов
    return await asyncio.to_thread(_hasher.hash, password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("dummy-password-for-timing")


async def _verify(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Проверка пароля. Второе значение — новый хэш, если параметры argon2 устарели."""
    return await asyncio.to_thread(_hasher.verify_and_update, password, password_hash)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = UserRepository(session)

    async def create(self, username: str, password: str, is_admin: bool = False) -> User:
        username = normalize_username(username)
        validate_password(password)
        if await self.repo.get_by_username(username):
            raise UserError(f"Пользователь {username} уже есть")
        return await self.repo.add(username, await hash_password(password), is_admin)

    async def authenticate(self, username: str, password: str) -> User | None:
        """Пользователь, если логин и пароль верны и он не отключён. Иначе None."""
        user = await self.repo.get_by_username(username.strip().lower())
        if user is None:
            # Тратим то же время, что и на настоящую проверку: по времени ответа
            # нельзя узнать, существует ли такой логин
            await _verify(password, _dummy_hash())
            return None
        valid, new_hash = await _verify(password, user.password_hash)
        if not valid or not user.is_active:
            return None
        if new_hash:
            user.password_hash = new_hash
        user.last_login_at = datetime.now(UTC)
        return user

    @staticmethod
    def _not_self(actor: User, target: User, action: str) -> None:
        # Действующий администратор себя не меняет — значит, хотя бы один администратор всегда остаётся
        if actor.id == target.id:
            raise UserError(f"Нельзя {action} самого себя: так можно остаться без администратора")

    async def set_active(self, actor: User, target: User, active: bool) -> None:
        self._not_self(actor, target, "отключить")
        target.is_active = active  # отключённого сразу выбрасывает: current_user проверяет is_active

    async def set_admin(self, actor: User, target: User, is_admin: bool) -> None:
        self._not_self(actor, target, "лишить прав администратора")
        target.is_admin = is_admin

    async def delete(self, actor: User, target: User) -> None:
        self._not_self(actor, target, "удалить")
        await self.repo.session.delete(target)

    async def set_password(self, user: User, password: str) -> None:
        """Сменить пароль. Все прежние входы пользователя перестают действовать."""
        validate_password(password)
        user.password_hash = await hash_password(password)
        user.session_version += 1
