"""Telegram: слушающий сборщик служебного аккаунта (этап 2 ТЗ).

Подписками управляет аккаунт: Сито слушает новые сообщения всех каналов, на которые он подписан,
и заводит источник с первым постом нового канала. Каналами управляют и из Сито:
- канал по ссылке — источник без id канала (pending), аккаунт подпишется сам, по одному в минуту;
- команды из веба (ConnectorCommand): показать канал по ссылке перед подпиской, отписаться;
- чтобы телефон служебного аккаунта не сходил с ума, каналы приглушаются, уходят в архив
  и отмечаются прочитанными (настройка подключения quiet).

Здесь только логика; работа с Telegram — за протоколом TelegramApi (реализация на Telethon —
telegram_client.py), чтобы логику можно было проверить без сети.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from sito.services.collector.base import (
    Command,
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
_HASH = re.compile(r"[A-Za-z0-9_-]{8,}")
_LINK = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?(.+?)/?(?:[?#].*)?$", re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Link:
    """Ссылка из Telegram: открытый канал, приглашение в закрытый или папка каналов."""

    kind: str   # channel, invite, folder
    value: str  # имя канала, код приглашения или код папки

    @property
    def url(self) -> str:
        return {"channel": f"https://t.me/{self.value}", "invite": f"https://t.me/+{self.value}",
                "folder": f"https://t.me/addlist/{self.value}"}[self.kind]


def parse_link(text: str) -> Link:
    """t.me/name, t.me/s/name, @name, name, t.me/+код, t.me/joinchat/код, t.me/addlist/код."""
    text = text.strip()
    match = _LINK.match(text)
    path = match.group(1) if match else text.removeprefix("@")
    if path.startswith("+") or path.lower().startswith("joinchat/"):
        code = path.removeprefix("+").split("/", 1)[-1] if path.startswith("+") else path.split("/", 1)[1]
        if _HASH.fullmatch(code):
            return Link("invite", code)
    elif path.lower().startswith("addlist/"):
        code = path.split("/", 1)[1]
        if _HASH.fullmatch(code):
            return Link("folder", code)
    elif _USERNAME.fullmatch(path):
        return Link("channel", path)
    raise ValueError(f"«{text}» — не ссылка на канал: нужна t.me/имя_канала, @имя, t.me/+приглашение или t.me/addlist/папка")


def parse_links(text: str) -> tuple[list[Link], list[str]]:
    """Несколько ссылок через пробел, запятую или с новой строки: (разобранные без повторов, ошибки)."""
    links: dict[str, Link] = {}
    errors: list[str] = []
    for item in re.split(r"[\s,;]+", text):
        if not item:
            continue
        try:
            link = parse_link(item)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        links.setdefault(link.url.lower(), link)
    return list(links.values()), errors


def channel_username(link: str) -> str:
    """Имя открытого канала из ссылки."""
    parsed = parse_link(link)
    if parsed.kind != "channel":
        raise ValueError("Нужна ссылка на открытый канал: t.me/имя_канала или @имя_канала")
    return parsed.value


def channel_url(username: str) -> str:
    return f"https://t.me/{username}"


class TelegramSourceForm(BaseModel):
    """Канал по ссылке: аккаунт подпишется на него сам."""

    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(title="Ссылка на канал", description="t.me/имя_канала, @имя_канала или t.me/+приглашение")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        link = parse_link(value)
        if link.kind == "folder":
            raise ValueError("Папку каналов добавляйте на странице каналов: там можно выбрать, какие из них нужны")
        return link.url


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


@dataclass(frozen=True, slots=True)
class ChannelPreview:
    """Канал перед подпиской: чтобы было видно, тот ли это канал."""

    link: str                     # по какой ссылке подписываться (t.me/имя или t.me/+приглашение)
    title: str
    username: str | None = None
    channel_id: int | None = None
    subscribers: int | None = None
    about: str = ""
    recent: list[str] = field(default_factory=list)  # начала последних постов
    subscribed: bool = False      # аккаунт уже подписан
    error: str | None = None      # почему подписаться нельзя (группа, а не канал; закрытый канал из папки)


@dataclass(frozen=True, slots=True)
class ChannelDetails:
    subscribers: int | None
    about: str


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

    async def preview(self, link: Link) -> list[ChannelPreview]:
        """Что за канал по ссылке (у папки — все её каналы). JoinError — ссылка не работает."""
        ...

    async def join(self, link: Link) -> Channel:
        """Подписаться на канал по ссылке (открытый или приглашение). JoinError — не вышло."""
        ...

    async def leave(self, channel_id: int) -> None:
        """Отписаться от канала."""
        ...

    async def search(self, query: str) -> list[ChannelPreview]:
        """Открытые каналы по словам — глобальный поиск Telegram."""
        ...

    async def similar(self, channel_id: int) -> list[ChannelPreview]:
        """«Похожие каналы», которые Telegram рекомендует к этому."""
        ...

    async def log_out(self) -> None:
        """Выйти из аккаунта: сессия завершается и на стороне Telegram."""
        ...

    async def details(self, channel_id: int) -> ChannelDetails:
        """Подписчики и описание канала."""
        ...

    async def quiet(self, channel_id: int) -> None:
        """Без звука и в архив: чтобы телефон служебного аккаунта не звенел от сотни каналов."""
        ...

    async def mark_read(self, channel_id: int, max_id: int) -> None:
        """Отметить прочитанным до max_id: на телефоне не копятся тысячи непрочитанных."""
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
    # Как часто сверять источники и делать медленную работу. За один круг — не больше одной подписки
    # на канал: массовые подписки подряд Telegram ограничивает (FloodWait)
    refresh_interval: float = 60
    command_interval: float = 2              # как часто смотреть поручения из веба
    batch: int = 100                         # постов в одной записи при догрузке
    per_round: int = 5                       # сведений о каналах за круг
    read_per_round: int = 20                 # отметок «прочитано» за круг
    details_every: timedelta = timedelta(days=1)


class TelegramListener(ConnectorListener):
    def __init__(self, connect: Connect, rules: TelegramRules | None = None) -> None:
        self.connect = connect
        self.rules = rules or TelegramRules()

    async def run(self, connector: ConnectorConfig, sink: PostSink) -> None:
        async with self.connect(connector) as api:
            await _Session(connector, sink, api, self.rules).run()


class _Session:
    """Одно соединение подключения: догрузка пропущенного, новые посты, подписки, поручения из веба."""

    def __init__(self, connector: ConnectorConfig, sink: PostSink, api: TelegramApi, rules: TelegramRules) -> None:
        self.connector = connector
        self.sink = sink
        self.api = api
        self.rules = rules
        self.sources: dict[str, KnownSource] = {}
        self.last_ids: dict[int, int] = {}  # канал → последний сохранённый id: курсор только растёт
        self.read_ids: dict[int, int] = {}  # канал → до какого id отмечено прочитанным
        self.locks: dict[int, asyncio.Lock] = {}
        # Каналы, на которые подписались в приложении: собирать сразу или ждать решения в Сито
        self.collect_new = connector.settings.get("new_channels", "collect") == "collect"
        self.keep_quiet = bool(connector.settings.get("quiet", True))

    async def run(self) -> None:
        await self.sink.connected(self.connector.id)
        await self._refresh_sources()
        # Новые сообщения слушаем ещё до догрузки: то, что придёт во время неё, не потеряется,
        # а пересечения не страшны — пост с тем же id просто обновится
        self.api.on_message(self._on_message)
        commands = asyncio.create_task(self._commands())  # поручения из веба — и во время долгой догрузки
        try:
            for channel in await self.api.channels():
                await self._catch_up(channel)
            logger.info("connector=%s: догрузка закончена, слушаю", self.connector.id)
            maintenance = asyncio.create_task(self._maintenance())
            try:
                await self.api.disconnected()
            finally:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
        finally:
            commands.cancel()
            await asyncio.gather(commands, return_exceptions=True)

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
                url=channel_url(channel.username) if channel.username else None, enabled=self.collect_new,
            )
            self.sources[known.external_id] = known
            logger.info("connector=%s: новый канал «%s»%s", self.connector.id, channel.title,
                        "" if known.enabled else " (ждёт решения в Сито)")
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

    # --- Поручения из веба -------------------------------------------------------------------------

    async def _commands(self) -> None:
        while True:
            await asyncio.sleep(self.rules.command_interval)
            try:
                while (command := await self.sink.next_command(self.connector.id)) is not None:
                    await self._run_command(command)
            except Exception:
                logger.exception("connector=%s: ошибка поручений", self.connector.id)

    async def _run_command(self, command: Command) -> None:
        try:
            if command.kind == "preview":
                result: Any = await self._preview(str(command.payload.get("links", "")))
            elif command.kind == "search":
                result = self._found(await self.api.search(str(command.payload.get("query", ""))))
            elif command.kind == "similar":
                result = self._found(await self.api.similar(int(self._by_id(int(command.payload["source_id"])).external_id)))
            elif command.kind == "logout":
                # После выхода соединение закроется, подключение станет «нужен вход»
                await self.sink.finish_command(command.id, {})
                await self.api.log_out()
                return
            elif command.kind == "leave":
                result = await self._leave(int(command.payload["source_id"]), bool(command.payload.get("delete")))
            else:
                raise ValueError(f"Неизвестное поручение {command.kind}")
        except Exception as exc:
            logger.warning("connector=%s: поручение %s не выполнено: %s", self.connector.id, command.kind, exc,
                           exc_info=True)
            await self.sink.finish_command(command.id, error=str(exc) or type(exc).__name__)
            return
        await self.sink.finish_command(command.id, result)

    async def _preview(self, text: str) -> list[dict[str, Any]]:
        """Каналы по ссылкам — до подписки: название, подписчики, последние посты."""
        links, errors = parse_links(text)
        found: list[dict[str, Any]] = [{"link": "", "title": "", "error": e} for e in errors]
        for link in links:
            try:
                previews = await self.api.preview(link)
            except JoinError as exc:
                found.append({"link": link.url, "title": "", "error": str(exc)})
                continue
            found.extend(self._found(previews))
        return found

    def _found(self, previews: list[ChannelPreview]) -> list[dict[str, Any]]:
        """Для страницы: known — канал уже в списке Сито."""
        return [
            {**asdict(item), "known": item.channel_id is not None and str(item.channel_id) in self.sources}
            for item in previews
        ]

    def _by_id(self, source_id: int) -> KnownSource:
        source = next((s for s in self.sources.values() if s.id == source_id), None)
        if source is None:
            raise ValueError("Канал не найден: возможно, он уже удалён")
        return source

    async def _leave(self, source_id: int, delete: bool) -> dict[str, Any]:
        source = self._by_id(source_id)
        await self.api.leave(int(source.external_id))
        await self.sink.forget_source(source_id, delete=delete)
        if delete:
            self.sources.pop(source.external_id, None)
        logger.info("connector=%s: отписался от «%s»", self.connector.id, source.name)
        return {"name": source.name}

    # --- Медленная работа раз в минуту ---------------------------------------------------------------

    async def _maintenance(self) -> None:
        paused_until: dict[str, float] = {}  # шаг → когда Telegram разрешит его снова (FloodWait)
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.rules.refresh_interval)
            for step in (self._refresh_sources, self._join_one, self._update_details, self._quiet, self._mark_read):
                name = step.__name__
                if loop.time() < paused_until.get(name, 0):
                    continue
                try:
                    await step()
                except Exception as exc:
                    wait = getattr(exc, "seconds", None)  # FloodWait: Telegram просит не повторять столько секунд
                    if isinstance(wait, int):
                        paused_until[name] = loop.time() + wait
                        logger.warning("connector=%s: Telegram просит подождать %d мин, шаг %s отложен",
                                       self.connector.id, wait // 60 + 1, name)
                    else:
                        logger.exception("connector=%s: ошибка обслуживания (%s)", self.connector.id, name)

    async def _join_one(self) -> None:
        """Подписаться на один канал, добавленный ссылкой. По одному с паузой: так Telegram не ограничит."""
        pending: list[PendingSource] = await self.sink.pending_sources(self.connector.id)
        if not pending:
            return
        item = pending[0]
        try:
            channel = await self.api.join(parse_link(item.url))
        except (JoinError, ValueError) as exc:
            await self.sink.pending_failed(item.id, str(exc) or "Не удалось подписаться")
            return
        known = await self.sink.bind_pending(item.id, str(channel.id), name=channel.title)
        self.sources[known.external_id] = known
        logger.info("connector=%s: подписался на «%s»", self.connector.id, channel.title)
        await self._catch_up(channel)

    def _live(self) -> list[KnownSource]:
        """Каналы, на которые аккаунт подписан (не отписанные из Сито)."""
        return [s for s in self.sources.values() if not s.info.get("left")]

    async def _update_details(self) -> None:
        """Подписчики и описание — раз в сутки на канал, по нескольку за круг."""
        now = datetime.now(UTC)
        stale = [
            s for s in self._live()
            if not s.info.get("details_at")
            or now - datetime.fromisoformat(str(s.info["details_at"])) > self.rules.details_every
        ]
        for source in stale[: self.rules.per_round]:
            details = await self.api.details(int(source.external_id))
            await self.sink.update_info(source.id, {
                "subscribers": details.subscribers, "about": details.about, "details_at": now.isoformat(),
            })

    async def _quiet(self) -> None:
        if not self.keep_quiet:
            return
        # По одному каналу за круг: настройки уведомлений Telegram ограничивает строже всего
        for source in [s for s in self._live() if not s.info.get("quiet")][:1]:
            await self.api.quiet(int(source.external_id))
            await self.sink.update_info(source.id, {"quiet": True})

    async def _mark_read(self) -> None:
        if not self.keep_quiet:
            return
        behind = [(cid, last) for cid, last in self.last_ids.items() if last > self.read_ids.get(cid, 0)]
        for channel_id, last_id in behind[: self.rules.read_per_round]:
            await self.api.mark_read(channel_id, last_id)
            self.read_ids[channel_id] = last_id
