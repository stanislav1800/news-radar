"""Работа с Telegram через Telethon: соединение для worker'а (TelegramApi) и вход для веба.

Сессия — строка StringSession, хранится в секретах подключения зашифрованной. Пользуется ей
только worker: одну сессию нельзя держать открытой из двух мест, Telegram сбросит её
(AuthKeyDuplicated). Веб открывает соединение лишь на время шагов входа, когда worker
подключение не запускает (статус «нужен вход»).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import Channel as TlChannel
from telethon.tl.types import PeerChannel

from sito.services.collector.base import ConnectorConfig, NeedsLogin
from sito.services.collector.telegram import (
    Channel,
    Connect,
    JoinError,
    Message,
    MessageHandler,
    TelegramApi,
)

logger = logging.getLogger(__name__)

# Одинаковые данные устройства при каждом подключении: «новое устройство» каждый раз выглядело бы подозрительно
DEVICE: dict[str, str] = {"device_model": "Sito", "system_version": "Linux", "app_version": "Sito 1.0"}

# Сессия больше не действует: отозвана из приложения, аккаунт удалён или заблокирован
SESSION_GONE: tuple[type[Exception], ...] = (
    errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.SessionExpiredError,
    errors.UserDeactivatedError, errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError,
)


def _client(api_id: int, api_hash: str, session: str, flood_sleep: int) -> TelegramClient:
    client = TelegramClient(
        StringSession(session), api_id, api_hash, flood_sleep_threshold=flood_sleep, **DEVICE,
    )
    client.parse_mode = "html"  # message.text — HTML со ссылками: их достанет очистка
    return client


def _channel(entity: TlChannel) -> Channel:
    return Channel(id=entity.id, title=entity.title, username=entity.username)


def _message(message: Any) -> Message:
    return Message(id=message.id, date=message.date, html=message.text or "")


def _is_broadcast(entity: object) -> bool:
    return isinstance(entity, TlChannel) and bool(entity.broadcast)


class TelethonApi(TelegramApi):
    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def channels(self) -> list[Channel]:
        return [_channel(d.entity) async for d in self.client.iter_dialogs() if _is_broadcast(d.entity)]

    async def messages(self, channel: Channel, *, after_id: int | None, since: datetime) -> AsyncIterator[Message]:
        entity = await self.client.get_input_entity(PeerChannel(channel.id))
        # reverse=True — от старых к новым: если догрузка прервётся, курсор не перескочит пропущенное
        found = (
            self.client.iter_messages(entity, min_id=after_id, reverse=True) if after_id is not None
            else self.client.iter_messages(entity, offset_date=since, reverse=True)
        )
        async for message in found:
            if getattr(message, "message", None) is not None:  # служебные сообщения без текста не нужны
                yield _message(message)

    async def join(self, username: str) -> Channel:
        try:
            entity = await self.client.get_entity(username)
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError) as exc:
            raise JoinError(f"Канала @{username} нет") from exc
        if not _is_broadcast(entity):
            raise JoinError(f"@{username} — не канал (группа, бот или пользователь)")
        try:
            await self.client(JoinChannelRequest(entity))
        except errors.ChannelsTooMuchError as exc:
            raise JoinError("Аккаунт подписан на слишком много каналов: Telegram больше не даёт") from exc
        except (errors.ChannelPrivateError, errors.InviteRequestSentError) as exc:
            raise JoinError(f"Канал @{username} закрыт для подписки") from exc
        return _channel(entity)

    def on_message(self, handler: MessageHandler) -> None:
        async def handle(event: Any) -> None:
            chat = await event.get_chat()
            if _is_broadcast(chat):
                await handler(_channel(chat), _message(event.message))

        self.client.add_event_handler(handle, events.NewMessage())
        self.client.add_event_handler(handle, events.MessageEdited())

    async def disconnected(self) -> None:
        await self.client.disconnected


def telethon_connect(flood_sleep: int = 600) -> Connect:
    """Соединение подключения для TelegramListener. flood_sleep — до скольких секунд FloodWait просто ждать."""

    @asynccontextmanager
    async def connect(config: ConnectorConfig) -> AsyncIterator[TelegramApi]:
        session = config.secrets.get("session")
        if not session:
            raise NeedsLogin("Нужно войти в аккаунт: «Войти» в настройках подключения")
        client = _client(int(config.settings["api_id"]), str(config.secrets["api_hash"]), str(session), flood_sleep)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise NeedsLogin("Сессия больше не действует: войдите заново")
            yield TelethonApi(client)
        except SESSION_GONE as exc:
            raise NeedsLogin(f"Сессия больше не действует ({type(exc).__name__}): войдите заново") from exc
        finally:
            await client.disconnect()

    return connect


# --- Вход (веб) ------------------------------------------------------------------------------------


class LoginError(Exception):
    """Шаг входа не прошёл; текст — для человека."""


@dataclass(frozen=True, slots=True)
class CodeSent:
    session: str           # незавершённая сессия: тот же ключ нужен для следующего шага
    phone_code_hash: str


@dataclass(frozen=True, slots=True)
class SignIn:
    session: str
    needs_password: bool   # включена двухфакторная защита: нужен облачный пароль


_LOGIN_ERRORS: dict[type[Exception], str] = {
    errors.ApiIdInvalidError: "api_id или api_hash неверные: проверьте на my.telegram.org",
    errors.PhoneNumberInvalidError: "Неверный номер телефона",
    errors.PhoneNumberBannedError: "Этот номер заблокирован в Telegram",
    errors.PhoneNumberUnoccupiedError: "На этот номер нет аккаунта Telegram",
    errors.PhoneCodeInvalidError: "Код неверный",
    errors.PhoneCodeExpiredError: "Код устарел: запросите новый",
    errors.PasswordHashInvalidError: "Пароль неверный",
}


@asynccontextmanager
async def _login_client(api_id: int, api_hash: str, session: str) -> AsyncIterator[TelegramClient]:
    client = _client(api_id, api_hash, session, flood_sleep=0)
    try:
        await client.connect()
        yield client
    except errors.FloodWaitError as exc:
        raise LoginError(f"Telegram просит подождать {exc.seconds} с перед следующей попыткой") from exc
    except tuple(_LOGIN_ERRORS) as exc:
        raise LoginError(_LOGIN_ERRORS[type(exc)]) from exc
    finally:
        await client.disconnect()


class TelethonLogin:
    """Шаги входа. Между шагами веб хранит незавершённую сессию в секретах подключения."""

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> CodeSent:
        async with _login_client(api_id, api_hash, "") as client:
            sent = await client.send_code_request(phone)
            return CodeSent(_save(client), sent.phone_code_hash)

    async def sign_in_code(self, api_id: int, api_hash: str, session: str, phone: str, code: str,
                           phone_code_hash: str) -> SignIn:
        async with _login_client(api_id, api_hash, session) as client:
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except errors.SessionPasswordNeededError:
                return SignIn(_save(client), needs_password=True)
            return SignIn(_save(client), needs_password=False)

    async def sign_in_password(self, api_id: int, api_hash: str, session: str, password: str) -> SignIn:
        async with _login_client(api_id, api_hash, session) as client:
            await client.sign_in(password=password)
            return SignIn(_save(client), needs_password=False)


def _save(client: TelegramClient) -> str:
    saved: Callable[[], str] = client.session.save  # type: ignore[assignment]
    return saved()

