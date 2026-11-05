"""Основа для слушающих сборщиков: шифрование секретов, приёмник постов, присмотр за подключениями."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Connector, ConnectorStatus, Post, Source, SourceType
from sito.services.collector.base import (
    ConnectorConfig,
    ConnectorListener,
    NeedsLogin,
    PostSink,
    RawPost,
)
from sito.services.connectors import ConnectorSupervisor
from sito.services.ingest import IngestService
from sito.utils.crypto import SecretBox, SecretsError
from tests.conftest import MakeSource

BOX = SecretBox("test-secret-key-0123")


# --- Шифрование ----------------------------------------------------------------------------------

def test_secrets_roundtrip() -> None:
    token = BOX.encrypt({"session": "строка сессии", "api_hash": "abc"})

    assert b"abc" not in token and "сессии".encode() not in token
    assert BOX.decrypt(token) == {"session": "строка сессии", "api_hash": "abc"}


def test_wrong_key_is_a_clear_error() -> None:
    token = BOX.encrypt({"session": "x"})

    with pytest.raises(SecretsError, match="SITO_SECRET_KEY не тот"):
        SecretBox("another-secret-key-456").decrypt(token)


# --- Приёмник постов (IngestService как PostSink) -----------------------------------------------

async def add_connector(
    factory: async_sessionmaker[AsyncSession], name: str = "Аккаунт 1", *,
    secrets: dict[str, str] | None = None, enabled: bool = True, settings: dict[str, object] | None = None,
) -> int:
    async with factory() as session, session.begin():
        connector = Connector(
            type=SourceType.TELEGRAM, name=name, enabled=enabled, settings=settings or {},
            secrets=BOX.encrypt(secrets) if secrets else None,
        )
        session.add(connector)
        await session.flush()
        return connector.id


async def get_connector(factory: async_sessionmaker[AsyncSession], connector_id: int) -> Connector:
    async with factory() as session:
        connector = await session.get(Connector, connector_id)
        assert connector is not None
        return connector


async def test_sink_creates_source_once_across_connectors(session_factory: async_sessionmaker[AsyncSession]) -> None:
    sink = IngestService(session_factory, {})
    first = await add_connector(session_factory, "Аккаунт 1")
    second = await add_connector(session_factory, "Аккаунт 2")

    channel = await sink.source(first, "-100123", name="Уголь Кузбасса", url="https://t.me/coal")
    same = await sink.source(second, "-100123", name="Уголь Кузбасса (другое имя)")

    assert same.id == channel.id  # канал из двух аккаунтов не задваивается
    assert [s.external_id for s in await sink.sources(first)] == ["-100123"]
    assert await sink.sources(second) == []
    async with session_factory() as session:
        source = await session.get(Source, channel.id)
    assert source is not None and source.poll_interval is None and source.url == "https://t.me/coal"


async def test_sink_saves_posts_with_cursor(session_factory: async_sessionmaker[AsyncSession]) -> None:
    sink = IngestService(session_factory, {})
    connector_id = await add_connector(session_factory)
    channel = await sink.source(connector_id, "-100123", name="Канал")
    posts = [RawPost(channel.id, str(n), f"https://t.me/coal/{n}", None, "", f"Пост {n}") for n in (1, 2)]

    result = await sink.save(channel.id, posts, {"last_message_id": 2})
    again = await sink.save(channel.id, posts, {"last_message_id": 2})

    assert (result.inserted, again.inserted, again.unchanged) == (2, 0, 2)
    [known] = await sink.sources(connector_id)
    assert known.state == {"last_message_id": 2}
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Post)) == 2


# --- Присмотр за подключениями -------------------------------------------------------------------

@dataclass
class FakeListener(ConnectorListener):
    """Слушатель для тестов: записывает запуски; поведение задаёт тест."""

    started: list[ConnectorConfig] = field(default_factory=list)
    fail_with: BaseException | None = None
    cancelled: int = 0
    # Тест ждёт его, прежде чем снова трогать базу: в тестах у всех сессий одно соединение
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, connector: ConnectorConfig, sink: PostSink) -> None:
        self.started.append(connector)
        if self.fail_with is not None:
            raise self.fail_with
        try:
            await sink.connected(connector.id)
            self.ready.set()
            await asyncio.Event().wait()  # «держит соединение», пока не остановят
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def listener() -> FakeListener:
    return FakeListener()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def supervisor(
    session_factory: async_sessionmaker[AsyncSession], listener: FakeListener, clock: Clock
) -> AsyncIterator[ConnectorSupervisor]:
    sup = ConnectorSupervisor(
        session_factory, {SourceType.TELEGRAM: listener}, IngestService(session_factory, {}), BOX,
        retry_min=30, retry_max=600, clock=clock,
    )
    yield sup
    await sup.close()


async def settle(listener: FakeListener | None = None) -> None:
    """Дать запущенным задачам дойти до ожидания (listener — дождаться, что он подключился)."""
    if listener is not None:
        await asyncio.wait_for(listener.ready.wait(), timeout=2)
        listener.ready.clear()
    for _ in range(5):
        await asyncio.sleep(0)


async def test_starts_enabled_connectors_with_decrypted_secrets(
    session_factory: async_sessionmaker[AsyncSession], supervisor: ConnectorSupervisor, listener: FakeListener
) -> None:
    active = await add_connector(session_factory, secrets={"session": "s3cr3t"}, settings={"phone": "+7"})
    await add_connector(session_factory, "Выключенное", enabled=False)

    await supervisor.sync()
    await settle(listener)

    assert supervisor.running == {active}
    [config] = listener.started
    assert config.secrets == {"session": "s3cr3t"} and config.settings == {"phone": "+7"}
    assert (await get_connector(session_factory, active)).status == ConnectorStatus.ACTIVE

    await supervisor.sync()  # ничего не поменялось — не перезапускает
    assert len(listener.started) == 1


async def test_restarts_on_change_and_stops_on_disable(
    session_factory: async_sessionmaker[AsyncSession], supervisor: ConnectorSupervisor, listener: FakeListener
) -> None:
    connector_id = await add_connector(session_factory, secrets={"session": "old"})
    await supervisor.sync()
    await settle(listener)

    async with session_factory() as session, session.begin():
        connector = await session.get(Connector, connector_id)
        assert connector is not None
        connector.secrets = BOX.encrypt({"session": "new"})
    await supervisor.sync()
    await settle(listener)

    assert listener.cancelled == 1
    assert [c.secrets["session"] for c in listener.started] == ["old", "new"]

    async with session_factory() as session, session.begin():
        connector = await session.get(Connector, connector_id)
        assert connector is not None
        connector.enabled = False
    await supervisor.sync()

    assert supervisor.running == set() and listener.cancelled == 2


async def test_failure_is_retried_with_growing_pause(
    session_factory: async_sessionmaker[AsyncSession], supervisor: ConnectorSupervisor,
    listener: FakeListener, clock: Clock,
) -> None:
    listener.fail_with = ConnectionError("сеть недоступна")
    connector_id = await add_connector(session_factory)

    await supervisor.sync()
    await settle()
    await supervisor.sync()  # заметил падение
    connector = await get_connector(session_factory, connector_id)
    assert connector.status == ConnectorStatus.ERROR and "сеть недоступна" in (connector.last_error or "")

    clock.now += 29
    await supervisor.sync()
    await settle()
    assert len(listener.started) == 1  # пауза 30 с ещё не прошла

    clock.now += 2
    await supervisor.sync()
    await settle()
    await supervisor.sync()
    assert len(listener.started) == 2

    clock.now += 31
    await supervisor.sync()
    await settle()
    assert len(listener.started) == 2  # вторая пауза уже 60 с
    clock.now += 30
    await supervisor.sync()
    await settle()
    assert len(listener.started) == 3


async def test_needs_login_waits_for_admin(
    session_factory: async_sessionmaker[AsyncSession], supervisor: ConnectorSupervisor,
    listener: FakeListener, clock: Clock,
) -> None:
    listener.fail_with = NeedsLogin("сессию отозвали")
    connector_id = await add_connector(session_factory, secrets={"session": "old"})
    await supervisor.sync()
    await settle()
    await supervisor.sync()

    assert (await get_connector(session_factory, connector_id)).status == ConnectorStatus.NEEDS_LOGIN
    clock.now += 10_000
    await supervisor.sync()
    await settle()
    assert len(listener.started) == 1  # сам не перезапускается

    listener.fail_with = None
    async with session_factory() as session, session.begin():  # администратор вошёл заново
        connector = await session.get(Connector, connector_id)
        assert connector is not None
        connector.secrets = BOX.encrypt({"session": "new"})
    await supervisor.sync()
    await settle(listener)
    assert len(listener.started) == 2
    assert (await get_connector(session_factory, connector_id)).status == ConnectorStatus.ACTIVE


async def test_secrets_under_other_key_are_not_started(
    session_factory: async_sessionmaker[AsyncSession], supervisor: ConnectorSupervisor, listener: FakeListener
) -> None:
    async with session_factory() as session, session.begin():
        connector = Connector(
            type=SourceType.TELEGRAM, name="Чужой ключ",
            secrets=SecretBox("another-secret-key-456").encrypt({"session": "x"}),
        )
        session.add(connector)

    await supervisor.sync()
    await settle()

    assert listener.started == []
    saved = await get_connector(session_factory, connector.id)
    assert saved.status == ConnectorStatus.ERROR and "SITO_SECRET_KEY" in (saved.last_error or "")


async def test_silent_connector_is_restarted(
    session_factory: async_sessionmaker[AsyncSession], supervisor: ConnectorSupervisor, listener: FakeListener,
    clock: Clock, make_source: MakeSource,
) -> None:
    from datetime import timedelta

    listener.silence_limit, listener.watch_sources = timedelta(minutes=15), 1
    connector_id = await add_connector(session_factory)
    await make_source(name="Канал", type=SourceType.TELEGRAM, connector_id=connector_id)
    await supervisor.sync()
    await settle(listener)

    clock.now += 10 * 60  # ещё рано: работает меньше 15 минут
    await supervisor.sync()
    assert len(listener.started) == 1

    clock.now += 6 * 60  # 16 минут без единого поста: зависло — перезапустить
    await supervisor.sync()
    await settle(listener)
    assert len(listener.started) == 2 and listener.cancelled == 1  # поднялось снова — статус опять «работает»
