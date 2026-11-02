"""Мастер входа в Telegram-аккаунт подключения: телефон → код → пароль (если включена защита).

Незавершённая сессия и хэш кода между шагами лежат в секретах подключения (зашифрованы), поэтому
мастер переживает перезапуск веба. Пока вход не закончен, статус подключения «нужен вход»
и worker его не запускает: сессией в одно время пользуется только кто-то один.
"""

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Connector, ConnectorStatus
from sito.services.collector.telegram_client import CodeSent, LoginError, SignIn
from sito.services.connectors import ConnectorError, ConnectorService
from sito.utils.crypto import SecretBox

__all__ = ["LoginApi", "LoginError", "TelegramLoginService"]

# Ключи секретов мастера: убираются, когда вход закончен
LOGIN_KEYS = ("login_session", "phone_code_hash", "login_step")


class LoginApi(Protocol):
    async def send_code(self, api_id: int, api_hash: str, phone: str) -> CodeSent: ...

    async def sign_in_code(self, api_id: int, api_hash: str, session: str, phone: str, code: str,
                           phone_code_hash: str) -> SignIn: ...

    async def sign_in_password(self, api_id: int, api_hash: str, session: str, password: str) -> SignIn: ...


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
        secrets = self.connectors.current_secrets(connector)
        sent = await self.api.send_code(*self._app(connector, secrets), str(connector.settings["phone"]))
        self._save(connector, secrets, login_session=sent.session, phone_code_hash=sent.phone_code_hash,
                   login_step="code")

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
