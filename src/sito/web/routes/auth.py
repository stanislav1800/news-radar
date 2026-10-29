import logging
import math
from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sito.repositories.users import UserRepository
from sito.services.users import UserService
from sito.web.deps import DbSession
from sito.web.security import safe_next
from sito.web.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()


async def _render_login(
    request: Request, db: DbSession, *, next_url: str, error: str | None = None, username: str = "",
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    first_run = await UserRepository(db).count() == 0
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next_url, "error": error, "username": username, "first_run": first_run},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: DbSession, next: str = "/") -> Response:
    if request.session.get("user_id"):
        return RedirectResponse(safe_next(next), status.HTTP_303_SEE_OTHER)
    return await _render_login(request, db, next_url=safe_next(next))


@router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    limiter = request.app.state.login_limiter
    client_ip = request.client.host if request.client else "unknown"
    key = (client_ip, username.strip().lower())

    if (wait := limiter.seconds_left(key)) > 0:
        minutes = math.ceil(wait / 60)
        return await _render_login(
            request, db, next_url=safe_next(next), username=username,
            error=f"Слишком много неудачных попыток. Попробуйте через {minutes} мин.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    async with db.begin():
        user = await UserService(db).authenticate(username, password)
        user_id, session_version = (user.id, user.session_version) if user else (None, None)

    if user_id is None:
        limiter.record_failure(key)
        logger.warning("неудачный вход: логин=%r адрес=%s", key[1], client_ip, extra={"client_ip": client_ip})
        return await _render_login(
            request, db, next_url=safe_next(next), username=username,
            error="Неверный логин или пароль", status_code=status.HTTP_400_BAD_REQUEST,
        )

    limiter.reset(key)
    # Новая сессия при входе: старый CSRF-токен и прочее не переносятся
    request.session.clear()
    request.session.update({"user_id": user_id, "session_version": session_version})
    logger.info("вход: логин=%r адрес=%s", key[1], client_ip, extra={"client_ip": client_ip})
    return RedirectResponse(safe_next(next), status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    response = RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
    response.headers["HX-Redirect"] = "/login"
    return response

