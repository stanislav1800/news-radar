"""Настройки пользователя: свой пароль и часовой пояс."""

from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sito.repositories.users import UserRepository
from sito.services.users import UserError, UserService
from sito.web.deps import CurrentUser, DbSession
from sito.web.security import flash, safe_next
from sito.web.templating import templates

router = APIRouter(prefix="/settings")

# Часовые пояса России, с запада на восток. Время в ленте показывается в выбранном
TIMEZONES = {
    "Europe/Kaliningrad": "Калининград (МСК−1)",
    "Europe/Moscow": "Москва (МСК)",
    "Europe/Samara": "Самара (МСК+1)",
    "Asia/Yekaterinburg": "Екатеринбург (МСК+2)",
    "Asia/Omsk": "Омск (МСК+3)",
    "Asia/Novosibirsk": "Новосибирск (МСК+4)",
    "Asia/Novokuznetsk": "Кемерово, Новокузнецк (МСК+4)",
    "Asia/Krasnoyarsk": "Красноярск (МСК+4)",
    "Asia/Irkutsk": "Иркутск (МСК+5)",
    "Asia/Yakutsk": "Якутск (МСК+6)",
    "Asia/Vladivostok": "Владивосток (МСК+7)",
    "Asia/Magadan": "Магадан (МСК+8)",
    "Asia/Kamchatka": "Камчатка (МСК+9)",
}


def _back() -> RedirectResponse:
    return RedirectResponse("/settings", status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, db: DbSession, user: CurrentUser) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "timezones": TIMEZONES, "hidden_sources": await UserRepository(db).hidden_sources(user.id),
    })


@router.post("/timezone")
async def set_timezone(request: Request, db: DbSession, user: CurrentUser, timezone: Annotated[str, Form()]) -> Response:
    if timezone not in TIMEZONES:
        flash(request, "Такого часового пояса нет в списке")
        return _back()
    user.timezone = timezone
    await db.commit()
    flash(request, f"Часовой пояс: {TIMEZONES[timezone]}")
    return _back()


@router.post("/password")
async def change_password(
    request: Request, db: DbSession, user: CurrentUser,
    current: Annotated[str, Form()], new: Annotated[str, Form()],
) -> Response:
    service = UserService(db)
    if await service.authenticate(user.username, current) is None:
        flash(request, "Текущий пароль неверный")
        return _back()
    try:
        await service.set_password(user, new)
    except UserError as exc:
        flash(request, str(exc))
        return _back()
    await db.commit()
    # Смена пароля завершает все прежние входы; этот — оставляем, чтобы не просить войти заново
    request.session["session_version"] = user.session_version
    flash(request, "Пароль изменён. На других устройствах нужно будет войти заново.")
    return _back()


@router.post("/tabs")
async def set_tabs(
    request: Request, db: DbSession, user: CurrentUser,
    show_top: Annotated[bool, Form()] = False, show_all: Annotated[bool, Form()] = False,
) -> Response:
    user.show_top, user.show_all = show_top, show_all
    await db.commit()
    flash(request, "Вкладки сохранены")
    return _back()


@router.post("/hidden-sources")
async def hide_source(
    request: Request, db: DbSession, user: CurrentUser, source_id: Annotated[int, Form()],
    action: Annotated[str, Form()] = "hide", next: Annotated[str, Form()] = "",
) -> Response:
    """Скрыть источник из «Главного» и «Всех новостей» или вернуть его."""
    repo = UserRepository(db)
    if action == "show":
        await repo.unhide_source(user.id, source_id)
        message = "Источник снова показывается"
    else:
        await repo.hide_source(user.id, source_id)
        message = "Источник скрыт из «Главного» и «Всех новостей». Вернуть — в Настройках."
    await db.commit()
    flash(request, message)
    return RedirectResponse(safe_next(next) if next else "/settings", status.HTTP_303_SEE_OTHER)
