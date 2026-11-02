"""Telegram в админке: подключение, мастер входа (код, пароль), канал по ссылке. Telegram поддельный."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Connector, ConnectorStatus, Source
from sito.services.collector.telegram_client import (
    CodeSent,
    CodeUnavailable,
    LoginError,
    SignIn,
)
from sito.services.users import UserService
from sito.utils.crypto import SecretBox
from tests.web import PASSWORD, csrf_from, login

BOX = SecretBox("test-secret-key-0123")


@dataclass
class FakeLogin:
    password: str | None = None  # облачный пароль, если включена двухэтапная проверка
    resend_exhausted: bool = False
    scanned: asyncio.Event = field(default_factory=asyncio.Event)  # QR-код отсканировали в приложении
    calls: list[str] = field(default_factory=list)

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> CodeSent:
        self.calls.append(f"code {api_id} {api_hash} {phone}")
        return CodeSent("half-session", "hash-1", "сообщением в приложение", "СМС на телефон")

    async def resend_code(self, api_id: int, api_hash: str, session: str, phone: str,
                          phone_code_hash: str) -> CodeSent:
        self.calls.append(f"resend {phone_code_hash}")
        if self.resend_exhausted:
            raise CodeUnavailable("Все способы прислать код уже использованы")
        return CodeSent("half-session", "hash-1", "СМС на телефон", None)

    async def sign_in_code(self, api_id: int, api_hash: str, session: str, phone: str, code: str,
                           phone_code_hash: str) -> SignIn:
        if code != "12345" or session != "half-session" or phone_code_hash != "hash-1":
            raise LoginError("Код неверный")
        return SignIn("half-session-2", needs_password=True) if self.password else SignIn("full-session", False)

    async def qr_login(self, api_id: int, api_hash: str, on_url: Callable[[str, datetime], None]) -> SignIn:
        on_url("tg://login?token=abc", datetime.now(UTC))
        await self.scanned.wait()
        return SignIn("half-session-2", needs_password=True) if self.password else SignIn("qr-session", False)

    async def sign_in_password(self, api_id: int, api_hash: str, session: str, password: str) -> SignIn:
        if password != self.password or session != "half-session-2":
            raise LoginError("Пароль неверный")
        return SignIn("full-session", needs_password=False)


@pytest.fixture
def fake_login(app: FastAPI) -> FakeLogin:
    fake = FakeLogin()
    app.state.telegram_login = fake
    return fake


@pytest.fixture
async def admin(client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    async with session_factory() as session, session.begin():
        await UserService(session).create("boss", PASSWORD, is_admin=True)
    assert (await login(client, "boss")).status_code == 303
    return client


async def post(client: httpx.AsyncClient, path: str, **data: str) -> httpx.Response:
    token = await csrf_from(client, "/admin/connectors")
    return await client.post(path, data={"csrf_token": token, **data})


async def connector(factory: async_sessionmaker[AsyncSession]) -> Connector:
    async with factory() as session:
        found = await session.scalar(select(Connector).where(Connector.type == "telegram"))
        assert found is not None
        return found


async def add_account(client: httpx.AsyncClient) -> str:
    response = await post(client, "/admin/connectors?type=telegram", name="Мониторинг", api_id="12345",
                          api_hash="secret-hash", phone="+7 (999) 000-11-22")
    assert response.status_code == 303
    return response.headers["location"]


async def test_login_with_code_and_password(
    admin: httpx.AsyncClient, fake_login: FakeLogin, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_login.password = "cloud-pass"
    login_page = await add_account(admin)
    created = await connector(session_factory)
    assert login_page == f"/admin/connectors/{created.id}/login"
    assert created.status == ConnectorStatus.NEEDS_LOGIN and created.settings["phone"] == "+79990001122"
    assert "Прислать код" in (await admin.get(login_page)).text

    await post(admin, f"{login_page}/send-code")
    page = (await admin.get(login_page)).text
    assert "Код из Telegram" in page and "отправил код сообщением в приложение" in page
    assert "прислать СМС на телефон" in page

    await post(admin, f"{login_page}/send-code")  # код не пришёл — тот же запрос, следующим способом
    assert fake_login.calls == ["code 12345 secret-hash +79990001122", "resend hash-1"]
    assert "отправил код СМС на телефон" in (await admin.get(login_page)).text

    wrong = await post(admin, f"{login_page}/code", code="99999")
    assert wrong.status_code == 400 and "Код неверный" in wrong.text

    await post(admin, f"{login_page}/code", code="12 345")
    assert "Облачный пароль" in (await admin.get(login_page)).text

    done = await post(admin, f"{login_page}/password", password="cloud-pass")

    assert done.status_code == 303
    saved = await connector(session_factory)
    assert saved.status == ConnectorStatus.ACTIVE
    assert saved.secrets is not None and BOX.decrypt(saved.secrets) == {"api_hash": "secret-hash", "session": "full-session"}


async def test_settings_change_keeps_session_only_for_same_account(
    admin: httpx.AsyncClient, fake_login: FakeLogin, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    page = await add_account(admin)
    await post(admin, f"{page}/send-code")
    await post(admin, f"{page}/code", code="12345")
    connector_id = (await connector(session_factory)).id
    form = {"name": "Новое имя", "api_id": "12345", "api_hash": "", "phone": "+79990001122"}

    await post(admin, f"/admin/connectors/{connector_id}", **form)
    renamed = await connector(session_factory)
    assert renamed.secrets is not None and BOX.decrypt(renamed.secrets)["session"] == "full-session"
    assert renamed.status == ConnectorStatus.ACTIVE

    await post(admin, f"/admin/connectors/{connector_id}", **{**form, "phone": "+79990003344"})
    moved = await connector(session_factory)
    assert moved.secrets is not None and "session" not in BOX.decrypt(moved.secrets)
    assert moved.status == ConnectorStatus.NEEDS_LOGIN


async def test_add_channel_by_link(
    admin: httpx.AsyncClient, fake_login: FakeLogin, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_account(admin)
    connector_id = (await connector(session_factory)).id

    added = await post(admin, f"/admin/connectors/{connector_id}/sources", url="https://t.me/s/ilyaseredyuk")
    again = await post(admin, f"/admin/connectors/{connector_id}/sources", url="@ilyaseredyuk")
    bad = await post(admin, f"/admin/connectors/{connector_id}/sources", url="https://t.me/+invite")

    assert added.status_code == 303
    assert again.status_code == 400 and "уже есть" in again.text
    assert bad.status_code == 400 and "приглашения" in bad.text
    async with session_factory() as session:
        [source] = list(await session.scalars(select(Source).where(Source.connector_id == connector_id)))
    assert (source.name, source.url, source.external_id) == ("@ilyaseredyuk", "https://t.me/ilyaseredyuk", None)
    assert "ждёт подписки" in (await admin.get("/admin/connectors")).text


async def test_resend_falls_back_to_new_code(
    admin: httpx.AsyncClient, fake_login: FakeLogin, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Старый запрос кода исчерпан (прошло много времени) — «прислать ещё раз» запрашивает новый код."""
    page = await add_account(admin)
    await post(admin, f"{page}/send-code")
    fake_login.resend_exhausted = True

    response = await post(admin, f"{page}/send-code")

    assert response.status_code == 303
    assert [c.split()[0] for c in fake_login.calls] == ["code", "resend", "code"]


async def test_login_by_qr_code(
    app: FastAPI, admin: httpx.AsyncClient, fake_login: FakeLogin, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    page = await add_account(admin)
    connector_id = (await connector(session_factory)).id

    started = await post(admin, f"{page}/qr")
    await asyncio.sleep(0)  # фоновый вход успевает получить токен
    fragment = await admin.get(f"{page}/qr")

    assert started.headers["location"] == f"{page}?qr=1"
    assert "<svg" in fragment.text and "Подключить устройство" in fragment.text

    fake_login.scanned.set()
    attempt = app.state.qr_logins.get(connector_id)
    await attempt.task
    finished = await admin.get(f"{page}/qr")

    assert finished.headers["HX-Redirect"] == f"/admin/connectors#connector-{connector_id}"
    saved = await connector(session_factory)
    assert saved.status == ConnectorStatus.ACTIVE
    assert saved.secrets is not None and BOX.decrypt(saved.secrets)["session"] == "qr-session"
