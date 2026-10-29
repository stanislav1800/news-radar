"""Зависимости FastAPI: сессия базы, текущий пользователь, проверка CSRF."""

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import User
from sito.repositories.users import UserRepository
from sito.web.security import CSRF_FORM_FIELD, CSRF_HEADER, CSRF_SESSION_KEY

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LoginRequired(Exception):
    """Нужен вход. Обработчик в app.py перенаправляет на страницу логина."""


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


async def current_user(request: Request, db: DbSession) -> User:
    user_id = request.session.get("user_id")
    user = await UserRepository(db).get(user_id) if isinstance(user_id, int) else None
    # Пользователя отключили или сменили пароль — прежняя сессия больше не действует
    if user is None or not user.is_active or user.session_version != request.session.get("session_version"):
        request.session.clear()
        raise LoginRequired
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужны права администратора")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def verify_csrf(request: Request) -> None:
    """Каждый изменяющий запрос должен нести токен из сессии: в поле формы или в заголовке (HTMX).

    Чужой сайт не может прочитать токен, поэтому не может и отправить форму от имени пользователя.
    """
    if request.method not in UNSAFE_METHODS:
        return
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        form = await request.form()
        value = form.get(CSRF_FORM_FIELD)
        sent = value if isinstance(value, str) else None
    expected = request.session.get(CSRF_SESSION_KEY)
    if not sent or not expected or not secrets.compare_digest(sent, str(expected)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Форма устарела: обновите страницу и попробуйте снова")
