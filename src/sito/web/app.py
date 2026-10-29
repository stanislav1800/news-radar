"""Веб-приложение Сито.

Запуск: uv run sito web   (или uvicorn sito.web.app:create_app --factory --proxy-headers)
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from sito.config import Settings, settings
from sito.db.engine import async_session
from sito.web.deps import LoginRequired, verify_csrf
from sito.web.routes import auth, pages
from sito.web.security import LoginLimiter
from sito.web.templating import WEB_DIR

SESSION_COOKIE = "sito_session"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",            # страницы Сито нельзя встроить в чужой сайт
    "Referrer-Policy": "same-origin",     # адреса страниц Сито не утекают на внешние сайты
}


def create_app(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    app_settings: Settings | None = None,
) -> FastAPI:
    config = app_settings or settings
    if config.secret_key is None:
        raise RuntimeError(
            "Не задан SITO_SECRET_KEY: он нужен, чтобы подписывать cookie сессий. "
            "Сгенерировать: python -c \"import secrets; print(secrets.token_urlsafe(32))\" и добавить в .env"
        )

    app = FastAPI(
        title="Сито",
        docs_url=None, redoc_url=None, openapi_url=None,  # JSON API пока нет, документация не нужна
        dependencies=[Depends(verify_csrf)],
    )
    app.state.session_factory = session_factory or async_session
    app.state.login_limiter = LoginLimiter(
        max_failures=config.login_max_failures,
        lockout_seconds=timedelta(minutes=config.login_lockout_minutes).total_seconds(),
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key.get_secret_value(),
        session_cookie=SESSION_COOKIE,
        max_age=int(timedelta(days=config.session_max_age_days).total_seconds()),
        same_site="lax",
        https_only=config.session_cookie_secure,
    )

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        login_url = f"/login?next={quote(request.url.path)}"
        if request.headers.get("HX-Request"):
            # HTMX подменяет часть страницы; ему нужен заголовок, чтобы перейти на логин целиком
            return Response(status_code=401, headers={"HX-Redirect": login_url})
        return RedirectResponse(login_url, status_code=303)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    return app
