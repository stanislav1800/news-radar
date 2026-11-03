"""Работа с Telegram через Telethon: соединение для worker'а (TelegramApi) и вход для веба.

Сессия — строка StringSession, хранится в секретах подключения зашифрованной. Пользуется ей
только worker: одну сессию нельзя держать открытой из двух мест, Telegram сбросит её
(AuthKeyDuplicated). Веб открывает соединение лишь на время шагов входа, когда worker
подключение не запускает (статус «нужен вход»).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telethon import TelegramClient, errors, events, utils
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.functions.channels import (
    GetChannelRecommendationsRequest,
    GetFullChannelRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import Channel as TlChannel
from telethon.tl.types import PeerChannel

from sito.services.collector.base import ConnectorConfig, NeedsLogin
from sito.services.collector.telegram import (
    Channel,
    ChannelDetails,
    ChannelPreview,
    Connect,
    DeletedHandler,
    JoinError,
    Link,
    Message,
    MessageHandler,
    TelegramApi,
)

logger = logging.getLogger(__name__)

# Одинаковые данные устройства при каждом подключении: «новое устройство» каждый раз выглядело бы подозрительно
DEVICE: dict[str, str] = {"device_model": "Sito", "system_version": "Linux", "app_version": "Sito 1.0"}

PREVIEW_POSTS = 3     # сколько последних постов показать перед подпиской
PREVIEW_CHARS = 200
SEARCH_LIMIT = 30
MUTE_FOREVER = 2**31 - 1  # «без звука навсегда» в Telegram — самая дальняя дата
ARCHIVE_FOLDER = 1

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
    links = []
    forwarded = getattr(message, "fwd_from", None)
    if forwarded is not None and isinstance(forwarded.from_id, PeerChannel) and forwarded.channel_post:
        # Репост: ссылка на оригинал (в том же виде, как Сито строит адрес поста канала)
        chat = getattr(message.forward, "chat", None) if message.forward else None
        username = getattr(chat, "username", None)
        links.append(f"https://t.me/{username}/{forwarded.channel_post}" if username
                     else f"https://t.me/c/{forwarded.from_id.channel_id}/{forwarded.channel_post}")
    page_url = getattr(getattr(getattr(message, "media", None), "webpage", None), "url", None)
    if isinstance(page_url, str):
        links.append(page_url)  # предпросмотр ссылки: статья, о которой пост
    return Message(id=message.id, date=message.date, html=message.text or "", links=tuple(links))


def _is_broadcast(entity: object) -> bool:
    return isinstance(entity, TlChannel) and bool(entity.broadcast)


def _short_preview(chat: TlChannel) -> ChannelPreview:
    """Канал из списка (поиск, похожие, папка): без последних постов — так быстрее."""
    return ChannelPreview(
        link=f"https://t.me/{chat.username}" if chat.username else "", title=chat.title,
        username=chat.username, channel_id=chat.id, subscribers=chat.participants_count,
        subscribed=not chat.left,
        error=None if chat.username else "Закрытый канал: подпишитесь в приложении или по приглашению",
    )


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

    async def _entity(self, channel_id: int) -> Any:
        # Каналы аккаунта Telethon помнит после channels() и входящих сообщений
        return await self.client.get_input_entity(PeerChannel(channel_id))

    async def _public(self, username: str) -> Any:
        try:
            entity = await self.client.get_entity(username)
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError) as exc:
            raise JoinError(f"Канала @{username} нет") from exc
        if not _is_broadcast(entity):
            raise JoinError(f"@{username} — не канал (группа, бот или пользователь)")
        return entity

    async def preview(self, link: Link) -> list[ChannelPreview]:
        if link.kind == "channel":
            entity = await self._public(link.value)
            full = await self.client(GetFullChannelRequest(entity))
            recent = [m.message[:PREVIEW_CHARS] for m in await self.client.get_messages(entity, limit=6) if m.message]
            return [ChannelPreview(
                link=link.url, title=entity.title, username=entity.username, channel_id=entity.id,
                subscribers=full.full_chat.participants_count, about=full.full_chat.about or "",
                recent=recent[:PREVIEW_POSTS], subscribed=not entity.left,
            )]
        if link.kind == "invite":
            try:
                invite = await self.client(CheckChatInviteRequest(link.value))
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError, errors.InviteHashEmptyError) as exc:
                raise JoinError("Приглашение недействительно или устарело") from exc
            if isinstance(invite, (types.ChatInviteAlready, types.ChatInvitePeek)):
                chat = invite.chat
                return [ChannelPreview(link=link.url, title=chat.title, username=getattr(chat, "username", None),
                                       channel_id=chat.id, subscribers=getattr(chat, "participants_count", None),
                                       subscribed=isinstance(invite, types.ChatInviteAlready),
                                       error=None if _is_broadcast(chat) else "Это группа, а не канал")]
            return [ChannelPreview(link=link.url, title=invite.title, subscribers=invite.participants_count,
                                   about=invite.about or "",
                                   error=None if invite.broadcast else "Это группа, а не канал")]
        try:
            folder = await self.client(CheckChatlistInviteRequest(link.value))
        except errors.RPCError as exc:
            raise JoinError(f"Папка не открывается: {exc.message}") from exc
        # В папках бывают и группы: их Сито не читает
        found = [_short_preview(chat) for chat in folder.chats if _is_broadcast(chat)]
        if not found:
            raise JoinError("В папке нет каналов")
        return found

    async def join(self, link: Link) -> Channel:
        if link.kind == "channel":
            entity = await self._public(link.value)
            request: Any = JoinChannelRequest(entity)
        elif link.kind == "invite":
            entity, request = None, ImportChatInviteRequest(link.value)
        else:
            raise JoinError("На папку целиком не подписываемся: выберите каналы из неё")
        try:
            updates = await self.client(request)
        except errors.UserAlreadyParticipantError:
            updates = None  # уже подписан по приглашению: канал узнаем из проверки приглашения
        except errors.ChannelsTooMuchError as exc:
            raise JoinError("Аккаунт подписан на слишком много каналов: Telegram больше не даёт") from exc
        except errors.InviteRequestSentError as exc:
            raise JoinError("Канал пускает по заявке: она отправлена, подпишитесь, когда одобрят") from exc
        except (errors.ChannelPrivateError, errors.InviteHashExpiredError, errors.InviteHashInvalidError) as exc:
            raise JoinError("Канал закрыт или приглашение устарело") from exc
        if entity is None:
            chats = getattr(updates, "chats", None) or []
            if not chats:
                invite = await self.client(CheckChatInviteRequest(link.value))
                chats = [getattr(invite, "chat", None)]
            entity = chats[0]
            if not _is_broadcast(entity):
                raise JoinError("По приглашению — группа, а не канал")
        return _channel(entity)

    async def leave(self, channel_id: int) -> None:
        try:
            await self.client(LeaveChannelRequest(await self._entity(channel_id)))
        except (errors.UserNotParticipantError, errors.ChannelPrivateError):
            pass  # уже не подписан — то, что нужно

    async def search(self, query: str) -> list[ChannelPreview]:
        found = await self.client(SearchRequest(q=query, limit=SEARCH_LIMIT))
        return [_short_preview(chat) for chat in found.chats if _is_broadcast(chat)]

    async def similar(self, channel_id: int) -> list[ChannelPreview]:
        found = await self.client(GetChannelRecommendationsRequest(channel=await self._entity(channel_id)))
        return [_short_preview(chat) for chat in found.chats if _is_broadcast(chat)]

    async def log_out(self) -> None:
        await self.client.log_out()

    async def details(self, channel_id: int) -> ChannelDetails:
        full = await self.client(GetFullChannelRequest(await self._entity(channel_id)))
        return ChannelDetails(full.full_chat.participants_count, full.full_chat.about or "")

    async def quiet(self, channel_id: int) -> None:
        entity = await self._entity(channel_id)
        await self.client(UpdateNotifySettingsRequest(
            peer=types.InputNotifyPeer(entity), settings=types.InputPeerNotifySettings(mute_until=MUTE_FOREVER),
        ))
        await self.client.edit_folder(entity, ARCHIVE_FOLDER)

    async def mark_read(self, channel_id: int, max_id: int) -> None:
        await self.client.send_read_acknowledge(await self._entity(channel_id), max_id=max_id)

    def on_message(self, handler: MessageHandler) -> None:
        async def handle(event: Any) -> None:
            chat = await event.get_chat()
            if _is_broadcast(chat):
                await handler(_channel(chat), _message(event.message))

        self.client.add_event_handler(handle, events.NewMessage())
        self.client.add_event_handler(handle, events.MessageEdited())

    def on_deleted(self, handler: DeletedHandler) -> None:
        async def handle(event: Any) -> None:
            if event.chat_id is None:
                return  # удаление в личных чатах и группах без указания чата — не наше
            channel_id, kind = utils.resolve_id(event.chat_id)
            if kind is PeerChannel:
                await handler(channel_id, list(event.deleted_ids))

        self.client.add_event_handler(handle, events.MessageDeleted())

    async def notify(self, text: str) -> None:
        await self.client.send_message("me", text)

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


class CodeUnavailable(LoginError):
    """Прежний запрос кода исчерпан или устарел: переотправить нельзя, нужен новый запрос."""


@dataclass(frozen=True, slots=True)
class CodeSent:
    session: str           # незавершённая сессия: тот же ключ нужен для следующего шага
    phone_code_hash: str
    delivery: str = ""     # куда Telegram отправил код: «сообщением в приложение Telegram…»
    next_delivery: str | None = None  # как придёт код, если попросить ещё раз


# Куда Telegram отправляет код: он выбирает сам, по типу ответа
_DELIVERY: dict[str, str] = {
    "SentCodeTypeApp": "сообщением от «Telegram» в приложение, где этот аккаунт уже открыт (телефон, компьютер)",
    "SentCodeTypeSms": "СМС на телефон",
    "SentCodeTypeSmsWord": "СМС на телефон",
    "SentCodeTypeSmsPhrase": "СМС на телефон",
    "SentCodeTypeFirebaseSms": "СМС на телефон",
    "SentCodeTypeCall": "звонком: код продиктуют",
    "SentCodeTypeFlashCall": "сброшенным звонком: код — последние цифры номера",
    "SentCodeTypeMissedCall": "пропущенным звонком: код — последние цифры номера",
    "SentCodeTypeFragmentSms": "через Fragment (анонимный номер)",
    "SentCodeTypeEmailCode": "на почту, привязанную к аккаунту",
    "CodeTypeSms": "СМС на телефон",
    "CodeTypeCall": "звонком",
    "CodeTypeFlashCall": "сброшенным звонком",
    "CodeTypeMissedCall": "пропущенным звонком",
    "CodeTypeFragmentSms": "через Fragment",
}


def _code_sent(client: TelegramClient, sent: Any) -> CodeSent:
    """Ответ Telegram на запрос кода → куда пришёл код. Случаи, когда кода не будет, — LoginError."""
    kind = type(sent).__name__
    if kind == "SentCodeSuccess":
        return CodeSent(_save(client), "", "вход уже выполнен")
    if kind == "SentCodePaymentRequired":
        raise LoginError("Telegram требует оплатить СМС для входа с этого номера. Войдите в аккаунт "
                         "в приложении Telegram на любом устройстве — тогда код придёт туда бесплатно")
    sent_type = type(sent.type).__name__
    logger.info("telegram: код запрошен, способ=%s, следующий=%s", sent_type, type(sent.next_type).__name__)
    if sent_type == "SentCodeTypeSetUpEmailRequired":
        raise LoginError("Telegram просит сначала привязать к аккаунту почту для входа: в приложении "
                         "Telegram → Настройки → Конфиденциальность → Почта для входа. Затем запросите код снова")
    delivery = _DELIVERY.get(sent_type, sent_type)
    if sent_type == "SentCodeTypeEmailCode" and getattr(sent.type, "email_pattern", None):
        delivery = f"на почту {sent.type.email_pattern}"
    following = _DELIVERY.get(type(sent.next_type).__name__) if sent.next_type is not None else None
    return CodeSent(_save(client), sent.phone_code_hash, delivery, following)


@dataclass(frozen=True, slots=True)
class SignIn:
    session: str
    needs_password: bool   # включена двухфакторная защита: нужен облачный пароль


# Сколько ждать сканирования QR-кода, прежде чем сдаться
QR_TIMEOUT = 300

_LOGIN_ERRORS: dict[type[Exception], str] = {
    errors.ApiIdInvalidError: "api_id или api_hash неверные: проверьте на my.telegram.org",
    errors.PhoneNumberInvalidError: "Неверный номер телефона",
    errors.PhoneNumberBannedError: "Этот номер заблокирован в Telegram",
    errors.PhoneNumberUnoccupiedError: "На этот номер нет аккаунта Telegram",
    errors.PhoneCodeInvalidError: "Код неверный",
    errors.PhoneCodeExpiredError: "Код устарел: запросите новый",
    errors.SendCodeUnavailableError: "Все способы прислать код уже использованы: подождите и запросите код заново",
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
    except (errors.SendCodeUnavailableError, errors.PhoneCodeExpiredError) as exc:
        raise CodeUnavailable(_LOGIN_ERRORS[type(exc)]) from exc
    except tuple(_LOGIN_ERRORS) as exc:
        raise LoginError(_LOGIN_ERRORS[type(exc)]) from exc
    finally:
        await client.disconnect()


class TelethonLogin:
    """Шаги входа. Между шагами веб хранит незавершённую сессию в секретах подключения."""

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> CodeSent:
        async with _login_client(api_id, api_hash, "") as client:
            return _code_sent(client, await client.send_code_request(phone))

    async def resend_code(self, api_id: int, api_hash: str, session: str, phone: str,
                          phone_code_hash: str) -> CodeSent:
        """Прислать код ещё раз — следующим способом (например, СМС вместо сообщения в приложении)."""
        async with _login_client(api_id, api_hash, session) as client:
            return _code_sent(client, await client(ResendCodeRequest(phone, phone_code_hash)))

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

    async def qr_login(self, api_id: int, api_hash: str, on_url: Callable[[str, datetime], None],
                       timeout: float = QR_TIMEOUT) -> SignIn:
        """Вход по QR-коду: код подтверждения не нужен, вход подтверждают в приложении Telegram.

        on_url получает адрес tg://login?token=… для QR-кода и время, когда он устареет. Токен живёт
        около 30 секунд — пока никто не отсканировал, он пересоздаётся. Соединение держится открытым
        всё время ожидания: иначе Telegram не сообщит, что код отсканирован.
        """
        async with _login_client(api_id, api_hash, "") as client:
            qr = await client.qr_login()
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                on_url(qr.url, qr.expires)
                try:
                    await qr.wait()
                except TimeoutError:
                    if asyncio.get_running_loop().time() > deadline:
                        raise LoginError("QR-код так и не отсканировали: начните заново") from None
                    await qr.recreate()
                    continue
                except errors.SessionPasswordNeededError:
                    return SignIn(_save(client), needs_password=True)
                return SignIn(_save(client), needs_password=False)


def _save(client: TelegramClient) -> str:
    saved: Callable[[], str] = client.session.save  # type: ignore[assignment]
    return saved()

