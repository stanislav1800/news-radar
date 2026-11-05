"""Telegram-сборщик на поддельном Telegram: догрузка пропущенного, новые посты, подписка по ссылке."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import ConnectorCommand, Post, Source, SourceType
from sito.services.collector.base import ConnectorConfig
from sito.services.collector.telegram import (
    Channel,
    ChannelDetails,
    ChannelPreview,
    DeletedHandler,
    JoinError,
    Link,
    Message,
    MessageHandler,
    TelegramApi,
    TelegramListener,
    TelegramRules,
    TelegramSourceForm,
    _Session,
    channel_url,
    channel_username,
    parse_links,
)
from sito.services.ingest import IngestService
from sito.utils.text import (
    clean_html,
    embedding_plain,
    is_chatter,
    split_headline,
    tidy_title,
)
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

    calls: list[str] = field(default_factory=list)  # что сделано с каналами: leave, quiet, read, details

    async def preview(self, link: Link) -> list[ChannelPreview]:
        if link.value not in self.public:
            raise JoinError(f"Канала @{link.value} нет")
        channel = self.public[link.value]
        return [ChannelPreview(link=link.url, title=channel.title, username=channel.username,
                               channel_id=channel.id, subscribers=1200, recent=["Последний пост"],
                               subscribed=channel in self.subscribed)]

    async def join(self, link: Link) -> Channel:
        if link.value not in self.public:
            raise JoinError(f"Канала @{link.value} нет")
        channel = self.public[link.value]
        self.subscribed.append(channel)
        return channel

    async def leave(self, channel_id: int) -> None:
        self.calls.append(f"leave {channel_id}")
        self.subscribed = [c for c in self.subscribed if c.id != channel_id]

    async def search(self, query: str) -> list[ChannelPreview]:
        self.calls.append(f"search {query}")
        return [ChannelPreview(link=channel_url(c.username), title=c.title, username=c.username, channel_id=c.id)
                for c in self.public.values() if c.username and query.lower() in c.title.lower()]

    async def similar(self, channel_id: int) -> list[ChannelPreview]:
        self.calls.append(f"similar {channel_id}")
        return [ChannelPreview(link="https://t.me/mash", title="Mash", username="mash", channel_id=333)]

    async def log_out(self) -> None:
        self.calls.append("log_out")

    deleted_handler: DeletedHandler | None = None

    def on_deleted(self, handler: DeletedHandler) -> None:
        self.deleted_handler = handler

    async def notify(self, text: str) -> None:
        self.calls.append(f"notify {text}")

    async def details(self, channel_id: int) -> ChannelDetails:
        self.calls.append(f"details {channel_id}")
        return ChannelDetails(5000, "Про уголь")

    async def quiet(self, channel_id: int) -> None:
        self.calls.append(f"quiet {channel_id}")

    async def mark_read(self, channel_id: int, max_id: int) -> None:
        self.calls.append(f"read {channel_id} {max_id}")

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


def test_links_of_all_kinds() -> None:
    links, errors = parse_links(
        "t.me/coal, https://t.me/+AbCdEf12345\nhttps://t.me/joinchat/XyZ12345678 t.me/addlist/Folder123 "
        "@coal https://example.com/x"
    )
    assert [(link.kind, link.url) for link in links] == [
        ("channel", "https://t.me/coal"), ("invite", "https://t.me/+AbCdEf12345"),
        ("invite", "https://t.me/+XyZ12345678"), ("folder", "https://t.me/addlist/Folder123"),
    ]
    assert len(errors) == 1 and "example.com" in errors[0]


def test_channel_signature_is_cut() -> None:
    cleaned = clean_html("Отремонтировали 489 домов.\n\nИлья Середюк в МАКС. Подписаться")
    assert cleaned.text == "Отремонтировали 489 домов."
    assert clean_html("Скидки на шубы\n#реклама").is_ad


async def add_command(factory: async_sessionmaker[AsyncSession], connector_id: int, kind: str,
                      payload: dict[str, object]) -> int:
    async with factory() as session, session.begin():
        command = ConnectorCommand(connector_id=connector_id, kind=kind, payload=payload)
        session.add(command)
        await session.flush()
        return command.id


async def command_result(factory: async_sessionmaker[AsyncSession], command_id: int) -> ConnectorCommand:
    async with factory() as session:
        command = await session.get(ConnectorCommand, command_id)
        assert command is not None
        return command


async def test_preview_and_leave_commands(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    """Поручения из веба: показать каналы по ссылкам, отписаться (посты остаются) и отписаться с удалением."""
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL, QUIET], public={"coal": COAL}, history={COAL.id: [message(1)]})
    session = _Session(config, sink, tg, TelegramRules())
    await session.run()
    sources = {s.external_id: s for s in await sink.sources(config.id)}
    preview = await add_command(session_factory, config.id, "preview", {"links": "t.me/coal\nt.me/nobody bad"})
    leave = await add_command(session_factory, config.id, "leave", {"source_id": sources["111"].id})
    delete = await add_command(session_factory, config.id, "leave",
                               {"source_id": sources["222"].id, "delete": True})

    while (command := await sink.next_command(config.id)) is not None:
        await session._run_command(command)

    shown = (await command_result(session_factory, preview)).result
    assert [(item["title"], item.get("known"), item["error"]) for item in shown] == [
        ("", None, "«bad» — не ссылка на канал: нужна t.me/имя_канала, @имя, t.me/+приглашение или t.me/addlist/папка"),
        ("Уголь Кузбасса", True, None),
        ("", None, "Канала @nobody нет"),
    ]
    assert (await command_result(session_factory, leave)).status == "done"
    assert (await command_result(session_factory, delete)).status == "done"
    assert tg.calls == ["leave 111", "leave 222"]
    [kept] = await sink.sources(config.id)
    assert kept.external_id == "111" and not kept.enabled and kept.info["left"] is True
    assert [p[1] for p in await posts(session_factory)] == ["1"]  # посты отписанного канала остаются


async def test_new_channels_can_wait_for_decision(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    waiting = ConnectorConfig(id=config.id, type=config.type, name="", settings={"new_channels": "wait"}, secrets={})
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [message(1)]})

    await _Session(waiting, sink, tg, TelegramRules()).run()

    [source] = await sink.sources(config.id)
    assert not source.enabled and await posts(session_factory) == []


async def test_maintenance_keeps_phone_quiet(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    """Раз в круг: сведения о каналах, без звука и в архив, прочитано до последнего сохранённого."""
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [message(5)]})
    session = _Session(config, sink, tg, TelegramRules())
    await session.run()

    await session._refresh_sources()
    await session._update_details()
    await session._quiet()
    await session._mark_read()
    await session._refresh_sources()
    await session._update_details()  # второй круг: всё уже сделано
    await session._quiet()
    await session._mark_read()

    assert tg.calls == ["details 111", "quiet 111", "read 111 5"]
    [source] = await sink.sources(config.id)
    assert source.info["subscribers"] == 5000 and source.info["quiet"] is True


async def test_search_similar_and_logout_commands(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], public={"coal": COAL})
    session = _Session(config, sink, tg, TelegramRules())
    await session.run()
    [coal] = await sink.sources(config.id)
    search = await add_command(session_factory, config.id, "search", {"query": "уголь"})
    similar = await add_command(session_factory, config.id, "similar", {"source_id": coal.id})
    await add_command(session_factory, config.id, "logout", {})

    while (command := await sink.next_command(config.id)) is not None:
        await session._run_command(command)

    assert [(i["title"], i["known"]) for i in (await command_result(session_factory, search)).result] == [
        ("Уголь Кузбасса", True)]
    assert [i["title"] for i in (await command_result(session_factory, similar)).result] == ["Mash"]
    assert tg.calls == ["search уголь", "similar 111", "log_out"]


def test_headline_of_telegram_post() -> None:
    """Заголовок — жирная первая строка или отделённая пустой строкой; сплошной текст — без заголовка."""
    assert split_headline("<strong>В Иране пригрозили ударами</strong>\n<a href='x'>x</a>\n\nТекст") == (
        "<strong>В Иране пригрозили ударами</strong>", "<a href='x'>x</a>\n\nТекст")
    assert split_headline("🔥 ДИВЕРСИЯ ПОД ВИДОМ БИЗНЕСА?\n\nРостовский бизнесмен…") is not None
    assert split_headline("Евгений Шестернин покинул пост министра. Губернатор объяснил уход.") is None


def test_tidy_title() -> None:
    assert tidy_title("🔥 ДИВЕРСИЯ ПОД ВИДОМ БИЗНЕСА? ГОФ «КРАСНАЯ ЗВЕЗДА» НА ГРАНИ ГИБЕЛИ! 🔥") == (
        "Диверсия под видом бизнеса? ГОФ «Красная звезда» на грани гибели!")
    assert tidy_title("В ДНР открыли школу #новости") == "В ДНР открыли школу"


def test_embedding_text_without_noise() -> None:
    text = "🔥 Первый абзац про уголь #уголь\nhttps://t.me/x\nВторой абзац.\nТретий.\nЧетвёртый — рассуждения."
    assert embedding_plain(text) == "Первый абзац про уголь Второй абзац. Третий."


async def test_gap_after_failed_save_is_filled(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    """Сообщение не сохранилось, а следующие сдвинули курсор — канал догружается с пропущенного."""
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [message(1), message(2), message(3)]})
    session = _Session(config, sink, tg, TelegramRules())
    await session.run()
    async with session_factory() as db, db.begin():
        await db.execute(Post.__table__.delete().where(Post.external_id == "2"))  # будто 2 не сохранилось
    session.gaps[COAL.id] = 2

    await session._fill_gaps()

    assert [p[1] for p in await posts(session_factory)] == ["1", "2", "3"] and session.gaps == {}


async def test_silent_connection_is_restarted(setup: tuple[IngestService, ConnectorConfig]) -> None:
    sink, config = setup
    session = _Session(config, sink, FakeTelegram(subscribed=[COAL]), TelegramRules(silence_limit=timedelta(0),
                                                                                      watch_channels=1))
    await session.run()
    session.last_event = 0

    await session._check_alive()

    assert session.stale.is_set()


async def test_repost_keeps_link_to_original(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [
        Message(id=1, date=NOW, html="Репост новости", links=("https://t.me/tass_agency/77",)),
    ]})
    await _Session(config, sink, tg, TelegramRules()).run()

    async with session_factory() as db:
        post = await db.scalar(select(Post))
    assert post is not None and '<a href="https://t.me/tass_agency/77"></a>' in (post.raw_text or "")


@pytest.mark.parametrize(("text", "chatter"), [
    ("Доброе утро)", True),
    ("🧡 Ке акукека мака!", True),
    ("Всем хороших выходных! Берегите себя.", True),
    ("⚡️Взрывы в Белгороде", False),
    ("Курс доллара — 92 рубля", False),
    ("Сильный ветер повалил деревья, есть пострадавшие. Подробности позже, оставайтесь на связи с нами", False),
])
def test_chatter(text: str, chatter: bool) -> None:
    assert is_chatter(text) is chatter


async def test_deleted_post_leaves_and_notify_command(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [message(1), message(2)]})
    session = _Session(config, sink, tg, TelegramRules())
    await session.run()
    assert tg.deleted_handler is not None

    await tg.deleted_handler(COAL.id, [1])
    await add_command(session_factory, config.id, "notify", {"text": "Сломалось"})
    while (command := await sink.next_command(config.id)) is not None:
        await session._run_command(command)

    assert [p[1] for p in await posts(session_factory)] == ["2"]
    assert tg.calls == ["notify Сломалось"]


def test_hashtag_line_is_not_a_headline() -> None:
    raw = "#погрузка\n\nВ октябре погрузка на сети РЖД составила 95,6 млн тонн.\n\nПодробности"
    assert split_headline(raw) == ("В октябре погрузка на сети РЖД составила 95,6 млн тонн.", "\nПодробности")
    assert clean_html("#погрузка #РЖД\nТекст новости").text == "Текст новости"


async def test_live_message_during_catch_up_does_not_skip_missed_posts(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    """Пока догружается первый канал, во второй пришёл живой пост: пропущенное во втором всё равно догружается."""
    sink, config = setup
    tg = FakeTelegram(subscribed=[COAL], history={COAL.id: [message(1)]})
    await _Session(config, sink, tg, TelegramRules()).run()  # курсор угольного канала — 1

    # Простой: в угольном канале вышли 2 и 3. После переподключения первым догружается тихий канал,
    # и в это время в угольный приходит живой пост 4
    tg.history[COAL.id] += [message(2), message(3)]

    class Racing(FakeTelegram):
        async def messages(self, channel: Channel, *, after_id: int | None, since: datetime) -> AsyncIterator[Message]:
            if channel.id == QUIET.id and self.handler is not None:
                await self.handler(COAL, message(4))
            async for m in super().messages(channel, after_id=after_id, since=since):
                yield m

    racing = Racing(subscribed=[QUIET, COAL], history={**tg.history, QUIET.id: [message(10)]})
    await _Session(config, sink, racing, TelegramRules()).run()

    assert (COAL.id, 1) in racing.asked  # догрузка от курсора на момент подключения, а не от живого поста
    coal = [p[1] for p in await posts(session_factory) if p[0] == "Уголь Кузбасса"]
    assert coal == ["1", "2", "3", "4"]


async def test_broken_channel_does_not_stop_catch_up(
    session_factory: async_sessionmaker[AsyncSession], setup: tuple[IngestService, ConnectorConfig],
) -> None:
    sink, config = setup

    class Broken(FakeTelegram):
        async def messages(self, channel: Channel, *, after_id: int | None, since: datetime) -> AsyncIterator[Message]:
            if channel.id == QUIET.id:
                raise ValueError("канал заблокирован")
            async for m in super().messages(channel, after_id=after_id, since=since):
                yield m

    tg = Broken(subscribed=[QUIET, COAL], history={COAL.id: [message(1)]})
    await _Session(config, sink, tg, TelegramRules()).run()

    assert [p[1] for p in await posts(session_factory)] == ["1"]
