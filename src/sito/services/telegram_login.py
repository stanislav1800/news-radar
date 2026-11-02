"""Мастер входа в Telegram-аккаунт подключения: телефон → код → пароль (если включена защита).

Незавершённая сессия и хэш кода между шагами лежат в секретах подключения (зашифрованы), поэтому
мастер переживает перезапуск веба. Пока вход не закончен, статус подключения «нужен вход»
и worker его не запускает: сессией в одно время пользуется только кто-то один.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Connector, ConnectorStatus
from sito.services.collector.telegram_client import (
    CodeSent,
    CodeUnavailable,
    LoginError,
    SignIn,
)
from sito.services.connectors import ConnectorError, ConnectorService
from sito.utils.crypto import SecretBox

__all__ = ["LoginApi", "LoginError", "QrAttempt", "QrLogins", "TelegramLoginService"]

logger = logging.getLogger(__name__)

# Ключи секретов мастера: убираются, когда вход закончен
LOGIN_KEYS = ("login_session", "phone_code_hash", "login_step", "login_delivery", "login_next")


class LoginApi(Protocol):
    async def send_code(self, api_id: int, api_hash: str, phone: str) -> CodeSent: ...

    async def resend_code(self, api_id: int, api_hash: str, session: str, phone: str,
                          phone_code_hash: str) -> CodeSent: ...

    async def sign_in_code(self, api_id: int, api_hash: str, session: str, phone: str, code: str,
                           phone_code_hash: str) -> SignIn: ...

    async def sign_in_password(self, api_id: int, api_hash: str, session: str, password: str) -> SignIn: ...

    async def qr_login(self, api_id: int, api_hash: str, on_url: Callable[[str, datetime], None]) -> SignIn: ...


class TelegramLoginService:
    def __init__(self, session: AsyncSession, box: SecretBox | None, api: LoginApi) -> None:
        self.connectors = ConnectorService(session, box)
        self.api = api

    def step(self, connector: Connector) -> str:
        """Какой шаг показать: start — запросить код, code — ввести код, password — пароль, done — вошли."""
        secrets = self.connectors.current_secrets(connector)
        if secrets.get("login_step") in ("code", "password"):
            return str(secrets["login_step"])
        if secrets.get("session") and connector.status != ConnectorStatus.NEEDS_LOGIN:
            return "done"
        return "start"

    async def send_code(self, connector: Connector) -> None:
        """Прислать код. Если код уже запрошен — прислать ещё раз следующим способом (СМС, звонок)."""
        secrets = self.connectors.current_secrets(connector)
        phone = str(connector.settings["phone"])
        sent = None
        if secrets.get("login_step") == "code":
            try:
                sent = await self.api.resend_code(*self._app(connector, secrets), str(secrets["login_session"]),
                                                  phone, str(secrets["phone_code_hash"]))
            except CodeUnavailable:
                sent = None  # прежний запрос исчерпан — просто запросить новый код
        if sent is None:
            sent = await self.api.send_code(*self._app(connector, secrets), phone)
        self._save(connector, secrets, login_session=sent.session, phone_code_hash=sent.phone_code_hash,
                   login_step="code", login_delivery=sent.delivery, login_next=sent.next_delivery)

    def delivery(self, connector: Connector) -> tuple[str | None, str | None]:
        """Куда пришёл код и как придёт, если попросить ещё раз."""
        secrets = self.connectors.current_secrets(connector)
        return secrets.get("login_delivery"), secrets.get("login_next")

    async def enter_code(self, connector: Connector, code: str) -> bool:
        """True — вошли; False — нужен ещё облачный пароль."""
        secrets = self.connectors.current_secrets(connector)
        if secrets.get("login_step") != "code":
            raise LoginError("Сначала запросите код")
        result = await self.api.sign_in_code(
            *self._app(connector, secrets), str(secrets["login_session"]), str(connector.settings["phone"]),
            code.strip().replace(" ", ""), str(secrets["phone_code_hash"]),
        )
        return self._after_sign_in(connector, secrets, result)

    async def enter_password(self, connector: Connector, password: str) -> bool:
        secrets = self.connectors.current_secrets(connector)
        if secrets.get("login_step") != "password":
            raise LoginError("Сначала введите код")
        result = await self.api.sign_in_password(*self._app(connector, secrets), str(secrets["login_session"]), password)
        return self._after_sign_in(connector, secrets, result)

    def app_credentials(self, connector: Connector) -> tuple[int, str]:
        return self._app(connector, self.connectors.current_secrets(connector))

    def finish(self, connector: Connector, result: SignIn) -> bool:
        """Вход (по QR-коду) закончен: сохранить сессию или перейти к облачному паролю."""
        return self._after_sign_in(connector, self.connectors.current_secrets(connector), result)

    def cancel(self, connector: Connector) -> None:
        secrets = self.connectors.current_secrets(connector)
        self._save(connector, {k: v for k, v in secrets.items() if k not in LOGIN_KEYS})

    def _after_sign_in(self, connector: Connector, secrets: dict[str, Any], result: SignIn) -> bool:
        if result.needs_password:
            self._save(connector, secrets, login_session=result.session, login_step="password")
            return False
        done = {k: v for k, v in secrets.items() if k not in LOGIN_KEYS}
        self._save(connector, done, session=result.session)
        # Worker увидит новые секреты (отпечаток изменился) и подключится
        connector.status = ConnectorStatus.ACTIVE
        connector.last_error = None
        return True

    @staticmethod
    def _app(connector: Connector, secrets: dict[str, Any]) -> tuple[int, str]:
        if not secrets.get("api_hash"):
            raise ConnectorError("У подключения нет api_hash: заполните его в настройках подключения")
        return int(connector.settings["api_id"]), str(secrets["api_hash"])

    def _save(self, connector: Connector, secrets: dict[str, Any], **changes: Any) -> None:
        connector.secrets = self.connectors.encrypt({**secrets, **changes})
        if connector.status != ConnectorStatus.ACTIVE or changes.get("login_step"):
            connector.status = ConnectorStatus.NEEDS_LOGIN


# --- Вход по QR-коду --------------------------------------------------------------------------------


@dataclass
class QrAttempt:
    """Идущий вход по QR-коду: страница входа раз в пару секунд спрашивает его состояние."""

    url: str | None = None          # tg://login?token=… — что нарисовать в QR-коде
    expires: datetime | None = None
    status: str = "waiting"         # waiting, password (нужен облачный пароль), done, error
    error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class QrLogins:
    """Входы по QR-коду в процессе веба: соединение с Telegram держится, пока QR-код не отсканируют.

    Один вход на подключение; результат пишется в подключение так же, как у входа по коду.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], box: SecretBox | None,
                 api: Callable[[], LoginApi]) -> None:
        self.session_factory = session_factory
        self.box = box
        self.api = api  # функция: тесты подменяют app.state.telegram_login после создания приложения
        self.attempts: dict[int, QrAttempt] = {}

    def get(self, connector_id: int) -> QrAttempt | None:
        return self.attempts.get(connector_id)

    def start(self, connector_id: int, api_id: int, api_hash: str) -> QrAttempt:
        current = self.attempts.get(connector_id)
        if current is not None and current.status == "waiting" and current.task and not current.task.done():
            return current
        attempt = QrAttempt()
        attempt.task = asyncio.create_task(self._run(connector_id, api_id, api_hash, attempt),
                                           name=f"telegram-qr-{connector_id}")
        self.attempts[connector_id] = attempt
        return attempt

    def cancel(self, connector_id: int) -> None:
        attempt = self.attempts.pop(connector_id, None)
        if attempt is not None and attempt.task is not None:
            attempt.task.cancel()

    async def _run(self, connector_id: int, api_id: int, api_hash: str, attempt: QrAttempt) -> None:
        def show(url: str, expires: datetime) -> None:
            attempt.url, attempt.expires = url, expires

        try:
            result = await self.api().qr_login(api_id, api_hash, show)
            async with self.session_factory() as session, session.begin():
                connector = await session.get(Connector, connector_id)
                if connector is None:
                    raise LoginError("Подключение удалено")
                done = TelegramLoginService(session, self.box, self.api()).finish(connector, result)
            attempt.status = "done" if done else "password"
        except LoginError as exc:
            attempt.status, attempt.error = "error", str(exc)
        except Exception as exc:
            logger.exception("connector=%s: вход по QR-коду не удался", connector_id)
            attempt.status, attempt.error = "error", f"{type(exc).__name__}: {exc}"
