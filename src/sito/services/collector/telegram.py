"""Telegram: слушающий сборщик служебного аккаунта (этап 2 ТЗ).

Подписками управляет аккаунт: Сито слушает новые сообщения всех каналов, на которые он подписан,
и заводит источник с первым постом нового канала. Канал можно добавить и из Сито ссылкой:
источник без id канала (pending) — аккаунт подпишется на него сам, по одному с паузой.

Здесь только логика; работа с Telegram — за протоколом TelegramApi (реализация на Telethon —
telegram_client.py), чтобы логику можно было проверить без сети.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from sito.services.collector.base import (
    ConnectorConfig,
    ConnectorListener,
    KnownSource,
    PendingSource,
    PostSink,
    RawPost,
)

logger = logging.getLogger(__name__)


# --- Формы ----------------------------------------------------------------------------------------


class TelegramConnectorForm(BaseModel):
    """Подключение — служебный аккаунт. Вход (код, пароль) — отдельным мастером после сохранения."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(title="Название", description="Например, «Telegram — мониторинг»", min_length=1, max_length=200)
    api_id: int = Field(title="api_id", description="С my.telegram.org → API development tools", gt=0)
    api_hash: SecretStr = Field(title="api_hash", description="Там же, рядом с api_id")
    phone: str = Field(
        title="Телефон аккаунта", description="В международном формате: +79991234567. Лучше отдельный аккаунт, не личный",
    )

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        digits = re.sub(r"[\s()-]", "", value)
        if not re.fullmatch(r"\+?\d{10,15}", digits):
            raise ValueError("Телефон в формате +79991234567")
        return "+" + digits.lstrip("+")


_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([^/?#]+)/?(?:[?#].*)?$", re.IGNORECASE)


def channel_username(link: str) -> str:
    """Имя канала из ссылки: t.me/name, https://t.me/s/name, @name или просто name."""
    link = link.strip()
    match = _LINK.match(link)
    name = match.group(1) if match else link.removeprefix("@")
    if name.startswith("+") or name == "joinchat":
        raise ValueError("Ссылки-приглашения в закрытые каналы пока не поддерживаются: нужна публичная ссылка t.me/имя")
    if not _USERNAME.fullmatch(name):
        raise ValueError("Нужна ссылка на канал: t.me/имя_канала или @имя_канала")
    return name


def channel_url(username: str) -> str:
    return f"https://t.me/{username}"


class TelegramSourceForm(BaseModel):
    """Канал по ссылке: аккаунт подпишется на него сам."""

    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(title="Ссылка на канал", description="t.me/имя_канала или @имя_канала — открытый канал")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return channel_url(channel_username(value))


# --- Что сборщику нужно от Telegram ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Channel:
    id: int                # id канала в Telegram: он и есть external_id источника
    title: str
    username: str | None   # у закрытого канала имени нет


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    date: datetime
    html: str              # текст с разметкой (ссылки нужны склейке); пустой — пост без текста


MessageHandler = Callable[[Channel, Message], Awaitable[None]]


class JoinError(Exception):
    """Подписаться не вышло: канала нет, это не канал, он закрыт или каналов у аккаунта слишком много."""


class TelegramApi(Protocol):
    """Подключённый и вошедший аккаунт."""

    async def channels(self) -> list[Channel]:
        """Каналы, на которые подписан аккаунт (только каналы: не группы и не личные чаты)."""
        ...

    def messages(self, channel: Channel, *, after_id: int | None, since: datetime) -> AsyncIterator[Message]:
        """Сообщения канала от старых к новым: после after_id, а если его нет — начиная с since."""
        ...

    async def join(self, username: str) -> Channel:
        """Подписаться на открытый канал. JoinError — не вышло, текст для админки."""
        ...

    def on_message(self, handler: MessageHandler) -> None:
        """Новые и отредактированные сообщения каналов."""
        ...

    async def disconnected(self) -> None:
        """Ждать, пока соединение не закроется насовсем (Telethon сам переподключается при обрывах)."""
        ...


Connect = Callable[[ConnectorConfig], AbstractAsyncContextManager[TelegramApi]]


def post_url(channel: Channel, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    return f"https://t.me/c/{channel.id}/{message_id}"  # закрытый канал: ссылка работает у подписчиков


# --- Сборщик ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TelegramRules:
    catch_up: timedelta = timedelta(days=1)  # новый канал: сколько истории взять
    # Как часто сверять источники (выключенные, добавленные ссылкой). За один раз — не больше одной
    # подписки на канал: массовые подписки подряд Telegram ограничивает (FloodWait)
    refresh_interval: float = 60
    batch: int = 100                         # постов в одной записи при догрузке


class TelegramListener(ConnectorListener):
    def __init__(self, connect: Connect, rules: TelegramRules | None = None) -> None:
        self.connect = connect
        self.rules = rules or TelegramRules()

    async def run(self, connector: ConnectorConfig, sink: PostSink) -> None:
        async with self.connect(connector) as api:
            await _Session(connector, sink, api, self.rules).run()


class _Session:
    """Одно соединение подключения: догрузка пропущенного, новые посты, подписки по ссылкам."""

    def __init__(self, connector: ConnectorConfig, sink: PostSink, api: TelegramApi, rules: TelegramRules) -> None:
        self.connector = connector
        self.sink = sink
        self.api = api
        self.rules = rules
        self.sources: dict[str, KnownSource] = {}
        self.last_ids: dict[int, int] = {}  # канал → последний сохранённый id: курсор только растёт
        self.locks: dict[int, asyncio.Lock] = {}

    async def run(self) -> None:
        await self.sink.connected(self.connector.id)
        await self._refresh_sources()
        # Новые сообщения слушаем ещё до догрузки: то, что придёт во время неё, не потеряется,
        # а пересечения не страшны — пост с тем же id просто обновится
        self.api.on_message(self._on_message)
        for channel in await self.api.channels():
            await self._catch_up(channel)
        logger.info("connector=%s: догрузка закончена, слушаю", self.connector.id)

        maintenance = asyncio.create_task(self._maintenance())
        try:
            await self.api.disconnected()
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)

    async def _refresh_sources(self) -> None:
        self.sources = {s.external_id: s for s in await self.sink.sources(self.connector.id)}
        for source in self.sources.values():
            last_id = source.state.get("last_id")
            if isinstance(last_id, int):
                self.last_ids.setdefault(int(source.external_id), last_id)

    async def _source(self, channel: Channel) -> KnownSource:
        known = self.sources.get(str(channel.id))
        if known is None:
            known = await self.sink.source(
                self.connector.id, str(channel.id), name=channel.title,
                url=channel_url(channel.username) if channel.username else None,
            )
            self.sources[known.external_id] = known
            logger.info("connector=%s: новый канал «%s»", self.connector.id, channel.title)
        return known

    async def _catch_up(self, channel: Channel) -> None:
        """Догрузить пропущенное за время простоя; у нового канала — за последние rules.catch_up."""
        source = await self._source(channel)
        if not source.enabled:
            return
        since = datetime.now(UTC) - self.rules.catch_up
        batch: list[Message] = []
        async for message in self.api.messages(channel, after_id=self.last_ids.get(channel.id), since=since):
            batch.append(message)
            if len(batch) >= self.rules.batch:
                await self._save(channel, source, batch)
                batch = []
        if batch:
            await self._save(channel, source, batch)

    async def _on_message(self, channel: Channel, message: Message) -> None:
        try:
            source = await self._source(channel)
            if source.enabled:
                await self._save(channel, source, [message])
        except Exception:
            # Ошибка одного сообщения не должна рвать соединение: при следующем запуске оно догрузится
            logger.exception("connector=%s: не сохранилось сообщение %s канала %s",
                             self.connector.id, message.id, channel.id)

    async def _save(self, channel: Channel, source: KnownSource, messages: Sequence[Message]) -> None:
        posts = [
            RawPost(
                source_id=source.id, external_id=str(m.id), url=post_url(channel, m.id),
                published_at=m.date, title="", text=m.html,
            )
            for m in messages if m.html.strip()  # фото и видео без подписи склейке не помогут
        ]
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            last_id = max([self.last_ids.get(channel.id, 0), *(m.id for m in messages)])
            await self.sink.save(source.id, posts, {"last_id": last_id})
            self.last_ids[channel.id] = last_id

    async def _maintenance(self) -> None:
        """Раз в минуту: сверить источники (выключенные, добавленные ссылкой) и подписаться на новый канал."""
        while True:
            await asyncio.sleep(self.rules.refresh_interval)
            try:
                await self._refresh_sources()
                await self._join_one()
            except Exception:
                logger.exception("connector=%s: ошибка обслуживания", self.connector.id)

    async def _join_one(self) -> None:
        """Подписаться на один канал, добавленный ссылкой. По одному с паузой: так Telegram не ограничит."""
        pending: list[PendingSource] = await self.sink.pending_sources(self.connector.id)
        if not pending:
            return
        item = pending[0]
        try:
            channel = await self.api.join(item.url.rsplit("/", 1)[-1])
        except JoinError as exc:
            await self.sink.pending_failed(item.id, str(exc) or "Не удалось подписаться")
            return
        known = await self.sink.bind_pending(item.id, str(channel.id), name=channel.title)
        self.sources[known.external_id] = known
        logger.info("connector=%s: подписался на «%s»", self.connector.id, channel.title)
        await self._catch_up(channel)
