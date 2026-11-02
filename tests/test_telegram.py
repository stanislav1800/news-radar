"""Telegram-сборщик на поддельном Telegram: догрузка пропущенного, новые посты, подписка по ссылке."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, Source, SourceType
from sito.services.collector.base import ConnectorConfig
from sito.services.collector.telegram import (
    Channel,
    JoinError,
    Message,
    MessageHandler,
    TelegramApi,
    TelegramListener,
    TelegramRules,
    TelegramSourceForm,
    _Session,
    channel_username,
)
from sito.services.ingest import IngestService
from sito.utils.text import clean_html
from tests.test_connectors import add_connector

NOW = datetime.now(UTC)
COAL = Channel(id=111, title="Уголь Кузбасса", username="coal")
QUIET = Channel(id=222, title="Тихий канал", username=None)


def message(n: int, text: str = "Новость", hours_ago: float = 1) -> Message:
    return Message(id=n, date=NOW - timedelta(hours=hours_ago), html=f"{text} {n}" if text else "")


@dataclass
class FakeTelegram(TelegramApi):
    history: dict[int, list[Message]] = field(default_factory=dict)
    public: dict[str, Channel] = field(default_factory=dict)  # на что можно подписаться по имени
    subscribed: list[Channel] = field(default_factory=list)
    handler: MessageHandler | None = None
    asked: list[tuple[int, int | None]] = field(default_factory=list)  # (канал, after_id) догрузок

    async def channels(self) -> list[Channel]:
        return list(self.subscribed)

    async def messages(self, channel: Channel, *, after_id: int | None, since: datetime) -> AsyncIterator[Message]:
        self.asked.append((channel.id, after_id))
        for m in self.history.get(channel.id, []):
            if (m.id > after_id) if after_id is not None else (m.date >= since):
                yield m

    async def join(self, username: str) -> Channel:
        if username not in self.public:
            raise JoinError(f"Канала @{username} нет")
        channel = self.public[username]
        self.subscribed.append(channel)
        return channel

    def on_message(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def disconnected(self) -> None:
        return  # тест управляет сессией сам: соединение «закрывается» сразу после догрузки


CONFIG = ConnectorConfig(id=0, type=SourceType.TELEGRAM, name="Аккаунт", settings={}, secrets={})


async def posts(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, str | None]]:
    async with factory() as session:
        rows = await session.execute(
            select(Source.name, Post.external_id, Post.url).join(Source).order_by(Source.name, Post.external_id)
        )
        return [tuple(r) for r in rows]  # type: ignore[misc]


@pytest.fixture
async def setup(session_factory: async_sessionmaker[AsyncSession]) -> tuple[IngestService, ConnectorConfig]:
    connector_id = await add_connector(session_factory)
    return IngestService(session_factory, {}), ConnectorConfig(
        id=connector_id, type=CONFIG.type, name="Аккаунт", settings={}, secrets={},
    )


async def test_catch_up_takes_last_day_then_only_new(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    """Новый канал — за последние сутки; после перезапуска — только то, что после сохранённого."""
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [
        message(1, hours_ago=30), message(2), message(3, text=""),  # 3 — фото без подписи
    ]})

    await _Session(config, sink, tg, TelegramRules()).run()

    assert await posts(session_factory) == [("Уголь Кузбасса", "2", "https://t.me/coal/2")]

    tg.history[COAL.id].append(message(4))
    await _Session(config, sink, tg, TelegramRules()).run()

    assert tg.asked[-1] == (COAL.id, 3)  # курсор дошёл и до поста без текста: его не будут просить снова
    assert [p[1] for p in await posts(session_factory)] == ["2", "4"]


async def test_new_messages_and_closed_channel_links(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    tg = FakeTelegram(subscribed=[])
    await _Session(config, sink, tg, TelegramRules()).run()
    assert tg.handler is not None

    await tg.handler(QUIET, message(7))  # канал без имени: появился, когда аккаунт подписали в приложении
    await tg.handler(QUIET, message(7, text="Исправленная новость"))  # правка — тот же пост

    async with session_factory() as session:
        post = await session.scalar(select(Post))
    assert post is not None and post.url == "https://t.me/c/222/7" and post.raw_text == "Исправленная новость 7"
    [source] = await sink.sources(config.id)
    assert source.name == "Тихий канал" and source.state == {"last_id": 7}


async def test_disabled_channel_is_not_collected(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    known = await sink.source(config.id, str(COAL.id), name=COAL.title)
    async with session_factory() as session, session.begin():
        source = await session.get(Source, known.id)
        assert source is not None
        source.enabled = False
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [message(1)]})

    await _Session(config, sink, tg, TelegramRules()).run()
    assert tg.handler is not None
    await tg.handler(COAL, message(2))

    assert await posts(session_factory) == []


async def test_join_channel_added_by_link(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    """Канал по ссылке: аккаунт подписывается, источник получает id и название, история — за сутки."""
    sink, config = setup
    async with session_factory() as session, session.begin():
        for url in ("https://t.me/coal", "https://t.me/nobody"):
            session.add(Source(connector_id=config.id, type=SourceType.TELEGRAM, name=url, url=url, state={}))
    tg = FakeTelegram(public={"coal": COAL}, history={COAL.id: [message(1)]})
    session = _Session(config, sink, tg, TelegramRules())
    await session.run()

    await session._join_one()  # по одному каналу за круг
    await session._join_one()
    await session._join_one()

    async with session_factory() as db:
        sources = {s.url: s for s in await db.scalars(select(Source))}
    assert sources["https://t.me/coal"].external_id == "111" and sources["https://t.me/coal"].name == "Уголь Кузбасса"
    assert sources["https://t.me/nobody"].last_error == "Канала @nobody нет"  # больше не пробует
    assert [p[1] for p in await posts(session_factory)] == ["1"]
    assert tg.subscribed == [COAL]


async def test_link_to_channel_already_followed_is_merged(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    await sink.source(config.id, str(COAL.id), name=COAL.title, url="https://t.me/coal_old_name")
    async with session_factory() as session, session.begin():
        session.add(Source(connector_id=config.id, type=SourceType.TELEGRAM, name="@coal", url="https://t.me/coal", state={}))
    session = _Session(config, sink, FakeTelegram(public={"coal": COAL}), TelegramRules())

    await session._join_one()

    assert [s.external_id for s in await sink.sources(config.id)] == ["111"]  # без дубля
    async with session_factory() as db:
        assert len(list(await db.scalars(select(Source)))) == 1


async def test_listener_opens_and_closes_connection(setup: tuple[IngestService, ConnectorConfig]) -> None:
    sink, config = setup
    opened: list[int] = []

    @asynccontextmanager
    async def connect(connector: ConnectorConfig) -> AsyncIterator[TelegramApi]:
        opened.append(connector.id)
        yield FakeTelegram()
        opened.append(-connector.id)

    await TelegramListener(connect).run(config, sink)

    assert opened == [config.id, -config.id]


@pytest.mark.parametrize(("link", "name"), [
    ("https://t.me/ilyaseredyuk", "ilyaseredyuk"),
    ("t.me/s/tass_agency", "tass_agency"),
    ("@rbc_news", "rbc_news"),
    ("https://telegram.me/mash?single", "mash"),
])
def test_channel_link(link: str, name: str) -> None:
    assert channel_username(link) == name
    assert TelegramSourceForm(url=link).url == f"https://t.me/{name}"


@pytest.mark.parametrize("link", ["https://t.me/+AbCdEf123", "https://example.com/coal", "@ab"])
def test_bad_channel_link(link: str) -> None:
    with pytest.raises(ValueError):
        channel_username(link)


def test_channel_signature_is_cut() -> None:
    cleaned = clean_html("Отремонтировали 489 домов.\n\nИлья Середюк в МАКС. Подписаться")
    assert cleaned.text == "Отремонтировали 489 домов."
    assert clean_html("Скидки на шубы\n#реклама").is_ad
