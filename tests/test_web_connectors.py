"""«Подключения»: добавление подключения по форме из описания коллектора, секреты, правка, удаление.

Настоящего второго типа ещё нет, поэтому тест регистрирует поддельный «Telegram» с формой подключения.
"""

from collections.abc import Iterator

import httpx
import pytest
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Connector, ConnectorStatus, SourceType
from sito.services.collector import registry
from sito.services.collector.registry import CollectorSpec
from sito.services.users import UserService
from sito.utils.crypto import SecretBox
from tests.web import PASSWORD, csrf_from, login

BOX = SecretBox("test-secret-key-0123")  # тот же ключ, что у тестового приложения


class FakeConnectorForm(BaseModel):
    name: str = Field(title="Название", min_length=1)
    phone: str = Field(title="Телефон", min_length=5)
    api_hash: SecretStr = Field(title="api_hash")


@pytest.fixture(autouse=True)
def fake_type(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    spec = CollectorSpec(
        source_type=SourceType.TELEGRAM, title="Telegram", description="Каналы аккаунта",
        builtin=False, polled=False, connector_form=FakeConnectorForm,
    )
    monkeypatch.setitem(registry.COLLECTORS, SourceType.TELEGRAM, spec)
    yield


@pytest.fixture
async def admin(client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    async with session_factory() as session, session.begin():
        await UserService(session).create("boss", PASSWORD, is_admin=True)
    assert (await login(client, "boss")).status_code == 303
    return client


async def post(client: httpx.AsyncClient, path: str, **data: str) -> httpx.Response:
    token = await csrf_from(client, "/admin/connectors")
    return await client.post(path, data={"csrf_token": token, **data})


async def connectors(factory: async_sessionmaker[AsyncSession]) -> list[Connector]:
    async with factory() as session:
        return list(await session.scalars(select(Connector).where(Connector.builtin.is_(False))))


async def add(client: httpx.AsyncClient) -> httpx.Response:
    return await post(client, "/admin/connectors?type=telegram", name="Мониторинг", phone="+79990001122",
                      api_hash="very-secret-hash")


async def test_add_connector_encrypts_secrets(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    page = (await admin.get("/admin/connectors")).text
    assert "Добавить подключение: Telegram" in page and 'name="api_hash" type="password"' in page

    response = await add(admin)

    assert response.status_code == 303
    [connector] = await connectors(session_factory)
    assert connector.name == "Мониторинг" and connector.settings == {"phone": "+79990001122"}
    assert connector.secrets is not None and b"very-secret-hash" not in connector.secrets
    assert BOX.decrypt(connector.secrets) == {"api_hash": "very-secret-hash"}
    page = (await admin.get("/admin/connectors")).text
    assert "very-secret-hash" not in page  # секрет не показывается никогда
    assert "+79990001122" in page and "Сохранено. Оставьте пустым, чтобы не менять." in page
    assert "Источников пока нет: они появятся сами" in page


async def test_add_connector_validation(admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    response = await post(admin, "/admin/connectors?type=telegram", name="", phone="1", api_hash="x")

    assert response.status_code == 400
    assert "Обязательное поле" in response.text
    assert await connectors(session_factory) == []
    assert (await post(admin, "/admin/connectors?type=vk", name="x")).status_code == 404


async def test_edit_keeps_secret_when_left_empty(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add(admin)
    [connector] = await connectors(session_factory)
    async with session_factory() as session, session.begin():
        stored = await session.get(Connector, connector.id)
        assert stored is not None
        stored.status, stored.last_error = ConnectorStatus.NEEDS_LOGIN, "сессию отозвали"

    await post(admin, f"/admin/connectors/{connector.id}", name="Мониторинг 2", phone="+79990003344", api_hash="")
    [edited] = await connectors(session_factory)
    assert edited.name == "Мониторинг 2" and edited.settings == {"phone": "+79990003344"}
    assert edited.secrets is not None and BOX.decrypt(edited.secrets) == {"api_hash": "very-secret-hash"}
    assert edited.status == ConnectorStatus.ACTIVE and edited.last_error is None  # после правки — новая попытка

    await post(admin, f"/admin/connectors/{connector.id}", name="Мониторинг 2", phone="+79990003344", api_hash="new")
    [edited] = await connectors(session_factory)
    assert edited.secrets is not None and BOX.decrypt(edited.secrets) == {"api_hash": "new"}


async def test_disable_and_delete(admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> None:
    await add(admin)
    [connector] = await connectors(session_factory)

    await post(admin, f"/admin/connectors/{connector.id}/enabled", enabled="false")
    [disabled] = await connectors(session_factory)
    assert not disabled.enabled
    assert "Включить подключение" in (await admin.get("/admin/connectors")).text

    await post(admin, f"/admin/connectors/{connector.id}/delete")
    assert await connectors(session_factory) == []


async def test_connector_with_sources_is_not_deleted(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from sito.services.ingest import IngestService

    await add(admin)
    [connector] = await connectors(session_factory)
    await IngestService(session_factory, {}).source(connector.id, "-100123", name="Канал про уголь")

    await post(admin, f"/admin/connectors/{connector.id}/delete")

    assert len(await connectors(session_factory)) == 1
    page = (await admin.get("/admin/connectors")).text
    assert "У подключения есть источники" in page
    assert "Канал про уголь" in page and "сразу" in page  # источник без опроса по расписанию


async def test_builtin_rss_cannot_be_deleted(admin: httpx.AsyncClient) -> None:
    await post(admin, "/admin/connectors/1/delete")
    assert "Встроенное подключение удалить нельзя" in (await admin.get("/admin/connectors")).text
