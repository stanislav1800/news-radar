"""Страница «Пользователи»: создание, роли, отключение, смена пароля, защита от «запереть себя»."""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import User
from sito.repositories.users import UserRepository
from sito.services.users import UserService
from tests.web import PASSWORD, csrf_from, login


@pytest.fixture
async def admin(client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    async with session_factory() as session, session.begin():
        await UserService(session).create("boss", PASSWORD, is_admin=True)
    assert (await login(client, "boss")).status_code == 303
    return client


@pytest.fixture
async def other_client(app: FastAPI) -> httpx.AsyncClient:
    """Второй браузер: для входа обычного пользователя параллельно с администратором."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def get_user(factory: async_sessionmaker[AsyncSession], username: str) -> User | None:
    async with factory() as session:
        return await UserRepository(session).get_by_username(username)


async def post(client: httpx.AsyncClient, path: str, **data: str) -> httpx.Response:
    token = await csrf_from(client, "/admin/users")
    return await client.post(path, data={"csrf_token": token, **data})


async def test_page_needs_admin(logged_in: httpx.AsyncClient) -> None:
    assert (await logged_in.get("/admin/users")).status_code == 403


async def test_create_user(admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    response = await post(admin, "/admin/users", username="Masha", password="long-password-1", is_admin="on")

    assert response.status_code == 303
    created = await get_user(session_factory, "masha")
    assert created is not None and created.is_admin
    page = (await admin.get("/admin/users")).text
    assert "Создан администратор masha" in page
    assert "long-password-1" not in page


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [("ab", "long-password-1", "Логин"), ("masha", "short", "Пароль должен быть"), ("", "x", "Обязательное поле")],
)
async def test_create_user_validation(admin: httpx.AsyncClient, username: str, password: str, message: str) -> None:
    response = await post(admin, "/admin/users", username=username, password=password)

    assert response.status_code == 400
    assert message in response.text
    assert f'value="{password}"' not in response.text  # пароль не возвращается в форму


async def test_create_duplicate(admin: httpx.AsyncClient, user: str) -> None:
    response = await post(admin, "/admin/users", username="IVAN", password="long-password-1")

    assert response.status_code == 400
    assert "уже есть" in response.text


@pytest.mark.parametrize(
    ("action", "data", "message"),
    [
        ("active", {"active": "false"}, "Нельзя отключить самого себя"),
        ("admin", {"is_admin": "false"}, "Нельзя лишить прав администратора самого себя"),
        ("delete", {}, "Нельзя удалить самого себя"),
    ],
)
async def test_admin_cannot_lock_themselves_out(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    action: str, data: dict[str, str], message: str,
) -> None:
    me = await get_user(session_factory, "boss")
    assert me is not None

    await post(admin, f"/admin/users/{me.id}/{action}", **data)

    assert message in (await admin.get("/admin/users")).text
    me = await get_user(session_factory, "boss")
    assert me is not None and me.is_active and me.is_admin


async def test_disabled_user_is_logged_out_at_once(
    admin: httpx.AsyncClient, other_client: httpx.AsyncClient, user: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with other_client:
        await login(other_client, user)
        assert (await other_client.get("/")).status_code == 200
        target = await get_user(session_factory, user)
        assert target is not None

        await post(admin, f"/admin/users/{target.id}/active", active="false")

        assert (await other_client.get("/")).status_code == 303
        assert (await login(other_client, user)).status_code == 400


async def test_reset_password_ends_sessions(
    admin: httpx.AsyncClient, other_client: httpx.AsyncClient, user: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with other_client:
        await login(other_client, user)
        target = await get_user(session_factory, user)
        assert target is not None

        await post(admin, f"/admin/users/{target.id}/password", password="brand-new-pass")

        assert (await other_client.get("/")).status_code == 303
        assert (await login(other_client, user, "brand-new-pass")).status_code == 303


async def test_short_password_reset_is_rejected(
    admin: httpx.AsyncClient, user: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    target = await get_user(session_factory, user)
    assert target is not None

    await post(admin, f"/admin/users/{target.id}/password", password="short")

    assert "Пароль должен быть не короче 8" in (await admin.get("/admin/users")).text


async def test_promote_demote_and_delete(
    admin: httpx.AsyncClient, user: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    target = await get_user(session_factory, user)
    assert target is not None

    await post(admin, f"/admin/users/{target.id}/admin", is_admin="true")
    promoted = await get_user(session_factory, user)
    assert promoted is not None and promoted.is_admin

    await post(admin, f"/admin/users/{target.id}/delete")
    assert await get_user(session_factory, user) is None
    assert "Пользователь ivan удалён" in (await admin.get("/admin/users")).text


async def test_actions_require_csrf(admin: httpx.AsyncClient, user: str, session_factory: async_sessionmaker[AsyncSession]) -> None:
    target = await get_user(session_factory, user)
    assert target is not None

    assert (await admin.post(f"/admin/users/{target.id}/delete")).status_code == 403
    assert await get_user(session_factory, user) is not None
