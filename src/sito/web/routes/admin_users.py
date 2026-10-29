"""Страница администратора «Пользователи»."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from sito.models import User
from sito.repositories.users import UserRepository
from sito.services.users import UserCreateForm, UserError, UserService
from sito.web.deps import AdminUser, DbSession, require_admin
from sito.web.forms import form_fields, validation_errors
from sito.web.security import flash
from sito.web.templating import templates

router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_admin)])

USERS_URL = "/admin/users"


async def _render(
    request: Request, db: DbSession, user: User, *, values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None, status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "admin/users.html",
        {
            "user": user,
            "users": await UserRepository(db).list_all(),
            "fields": form_fields(UserCreateForm, values, errors),
            "form_error": (errors or {}).get("__all__"),
            "form_open": errors is not None,
            "now": datetime.now(UTC),
        },
        status_code=status_code,
    )


async def _target(db: DbSession, user_id: int) -> User:
    target = await UserRepository(db).get(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    return target


async def _apply(request: Request, db: DbSession, action: Callable[[], Awaitable[None]], done: str) -> Response:
    """Выполнить действие над пользователем: ошибку или результат показать сообщением наверху страницы."""
    try:
        await action()
    except UserError as exc:
        flash(request, str(exc))
    else:
        await db.commit()
        flash(request, done)
    return RedirectResponse(USERS_URL, status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
async def users_page(request: Request, db: DbSession, user: AdminUser) -> HTMLResponse:
    return await _render(request, db, user)


@router.post("")
async def create_user(request: Request, db: DbSession, user: AdminUser) -> Response:
    values = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        form = UserCreateForm.model_validate(values)
        created = await UserService(db).create(form.username, form.password.get_secret_value(), form.is_admin)
    except ValidationError as exc:
        return await _render(request, db, user, values=values, errors=validation_errors(exc),
                             status_code=status.HTTP_400_BAD_REQUEST)
    except UserError as exc:
        return await _render(request, db, user, values=values, errors={"__all__": str(exc)},
                             status_code=status.HTTP_400_BAD_REQUEST)
    await db.commit()
    role = "администратор" if created.is_admin else "пользователь"
    flash(request, f"Создан {role} {created.username}. Передайте ему логин и пароль.")
    return RedirectResponse(USERS_URL, status.HTTP_303_SEE_OTHER)


@router.post("/{user_id}/password")
async def reset_password(
    request: Request, db: DbSession, user: AdminUser, user_id: int, password: Annotated[str, Form()],
) -> Response:
    # Смена своего пароля тоже работает: текущая сессия перестанет действовать, и попросит войти заново
    target = await _target(db, user_id)
    return await _apply(
        request, db, lambda: UserService(db).set_password(target, password),
        f"Пароль {target.username} изменён. Все его прежние входы больше не действуют.",
    )


@router.post("/{user_id}/active")
async def set_active(
    request: Request, db: DbSession, user: AdminUser, user_id: int, active: Annotated[bool, Form()],
) -> Response:
    target = await _target(db, user_id)
    return await _apply(
        request, db, lambda: UserService(db).set_active(user, target, active),
        f"{target.username} {'включён' if active else 'отключён и больше не может войти'}.",
    )


@router.post("/{user_id}/admin")
async def set_admin(
    request: Request, db: DbSession, user: AdminUser, user_id: int, is_admin: Annotated[bool, Form()],
) -> Response:
    target = await _target(db, user_id)
    return await _apply(
        request, db, lambda: UserService(db).set_admin(user, target, is_admin),
        f"{target.username} {'теперь администратор' if is_admin else 'больше не администратор'}.",
    )


@router.post("/{user_id}/delete")
async def delete_user(request: Request, db: DbSession, user: AdminUser, user_id: int) -> Response:
    target = await _target(db, user_id)
    name = target.username
    return await _apply(request, db, lambda: UserService(db).delete(user, target), f"Пользователь {name} удалён.")
