"""Вход в веб: сессии, CSRF, ограничение попыток, перенаправления."""

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.config import settings
from sito.repositories.users import UserRepository
from sito.services.users import UserError, UserService
from sito.web.app import create_app
from tests.web import PASSWORD, csrf_from, login


async def test_pages_require_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"


async def test_htmx_gets_redirect_header(client: httpx.AsyncClient) -> None:
    response = await client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login?next=/"


async def test_first_run_hint(client: httpx.AsyncClient) -> None:
    assert "sito user create admin --admin" in (await client.get("/login")).text


async def test_login_and_logout(client: httpx.AsyncClient, user: str) -> None:
    response = await login(client, "IVAN ", PASSWORD)  # логин без учёта регистра и пробелов

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    page = await client.get("/")
    assert page.status_code == 200
    assert "ivan" in page.text

    token = await csrf_from(client, "/")
    assert (await client.post("/logout", data={"csrf_token": token})).status_code == 303
    assert (await client.get("/")).status_code == 303


async def test_wrong_password(client: httpx.AsyncClient, user: str) -> None:
    response = await login(client, user, "wrong-password")

    assert response.status_code == 400
    assert "Неверный логин или пароль" in response.text
    assert (await client.get("/")).status_code == 303


async def test_unknown_user_gets_same_message(client: httpx.AsyncClient, user: str) -> None:
    response = await login(client, "nobody", PASSWORD)

    assert response.status_code == 400
    assert "Неверный логин или пароль" in response.text


async def test_post_without_csrf_is_rejected(client: httpx.AsyncClient, user: str) -> None:
    await client.get("/login")  # сессия с токеном уже есть, но в форме его нет
    response = await client.post("/login", data={"username": user, "password": PASSWORD})

    assert response.status_code == 403


async def test_post_with_foreign_csrf_is_rejected(client: httpx.AsyncClient, user: str) -> None:
    await client.get("/login")
    response = await client.post(
        "/login", data={"username": user, "password": PASSWORD, "csrf_token": "token-from-attacker"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
async def test_next_cannot_lead_outside(client: httpx.AsyncClient, user: str, target: str) -> None:
    response = await login(client, user, PASSWORD, next=target)

    assert response.headers["location"] == "/"


async def test_next_inside_app_is_kept(client: httpx.AsyncClient, user: str) -> None:
    response = await login(client, user, PASSWORD, next="/stories/5")

    assert response.headers["location"] == "/stories/5"


async def test_lockout_after_failures(client: httpx.AsyncClient, user: str) -> None:
    for _ in range(settings.login_max_failures):
        assert (await login(client, user, "wrong-password")).status_code == 400

    response = await login(client, user, PASSWORD)  # даже верный пароль не пускает во время паузы

    assert response.status_code == 429
    assert "Слишком много неудачных попыток" in response.text


async def test_password_change_ends_old_sessions(
    client: httpx.AsyncClient, user: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await login(client, user, PASSWORD)
    assert (await client.get("/")).status_code == 200

    async with session_factory() as session, session.begin():
        db_user = await UserRepository(session).get_by_username(user)
        assert db_user is not None
        await UserService(session).set_password(db_user, "new-password-123")

    assert (await client.get("/")).status_code == 303


async def test_inactive_user_cannot_login(
    client: httpx.AsyncClient, user: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        db_user = await UserRepository(session).get_by_username(user)
        assert db_user is not None
        db_user.is_active = False

    assert (await login(client, user, PASSWORD)).status_code == 400


async def test_security_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/login")

    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_app_requires_secret_key(session_factory: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(RuntimeError, match="SITO_SECRET_KEY"):
        create_app(session_factory, settings.model_copy(update={"secret_key": None}))


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("ab", PASSWORD, "Логин"),
        ("иван", PASSWORD, "Логин"),
        ("ivan", "short", "Пароль"),
    ],
)
async def test_user_validation(
    session_factory: async_sessionmaker[AsyncSession], username: str, password: str, message: str
) -> None:
    async with session_factory() as session:
        with pytest.raises(UserError, match=message):
            await UserService(session).create(username, password)


async def test_duplicate_user(session_factory: async_sessionmaker[AsyncSession], user: str) -> None:
    async with session_factory() as session:
        with pytest.raises(UserError, match="уже есть"):
            await UserService(session).create("IVAN", PASSWORD)
