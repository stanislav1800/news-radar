"""Защита веба: ограничение попыток входа, CSRF-токены, безопасные адреса перехода."""

import asyncio
import secrets
import time
import zlib
from dataclasses import dataclass, field

from starlette.requests import Request

CSRF_SESSION_KEY = "csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0
    last_at: float = 0.0


LOGIN_WIDE_FACTOR = 4      # на один логин со всех адресов — вчетверо больше попыток
MAX_TRACKED = 10_000       # сколько записей держать в памяти, прежде чем чистить устаревшие
LOGIN_LOCKS = 64           # попытки на один логин идут по очереди; замков — фиксированное число
MAX_TRUSTED = 10_000       # адресов, с которых входили удачно (растёт только от удачных входов)


@dataclass
class LoginLimiter:
    """Пауза после нескольких неудачных попыток входа.

    Считается для пары «адрес + логин» и отдельно для логина со всех адресов: адрес клиента берётся
    из заголовка прокси, и перебор с подменой адреса упрётся во второй счётчик.

    Общий счётчик не блокирует адрес, с которого этот логин уже входил удачно: иначе чужой перебор
    запирал бы настоящего владельца. Перебор с такого адреса всё равно упрётся в счётчик пары.

    Хранится в памяти процесса: для одного процесса веба этого достаточно,
    после перезапуска счётчики обнуляются.
    """

    max_failures: int
    lockout_seconds: float
    _attempts: dict[tuple[str, str], _Attempts] = field(default_factory=dict)
    _locks: list[asyncio.Lock] = field(default_factory=lambda: [asyncio.Lock() for _ in range(LOGIN_LOCKS)])
    _trusted: set[tuple[str, str]] = field(default_factory=set)

    def lock(self, login: str) -> asyncio.Lock:
        """Замок логина: проверка паузы, пароля и запись неудачи — одним шагом. Без него сотня параллельных
        запросов прошла бы проверку паузы раньше, чем запишется первая неудача."""
        return self._locks[zlib.crc32(login.encode()) % LOGIN_LOCKS]

    @staticmethod
    def _keys(key: tuple[str, str]) -> tuple[tuple[str, str], tuple[str, str]]:
        return key, ("*", key[1])

    def seconds_left(self, key: tuple[str, str]) -> float:
        now = time.monotonic()
        keys = (key,) if key in self._trusted else self._keys(key)
        return max(
            (max(0.0, a.locked_until - now) for k in keys if (a := self._attempts.get(k))),
            default=0.0,
        )

    def record_failure(self, key: tuple[str, str]) -> None:
        if len(self._attempts) >= MAX_TRACKED:
            self._prune()
        for k in self._keys(key):
            limit = self.max_failures * (LOGIN_WIDE_FACTOR if k[0] == "*" else 1)
            attempts = self._attempts.setdefault(k, _Attempts())
            attempts.failures += 1
            attempts.last_at = time.monotonic()
            if attempts.failures >= limit:
                attempts.locked_until = time.monotonic() + self.lockout_seconds
                attempts.failures = 0

    def reset(self, key: tuple[str, str]) -> None:
        """Удачный вход: счётчик пары обнулить, адрес запомнить как свой для этого логина.

        Общий счётчик логина не обнуляется: удачный вход владельца не должен давать перебору
        с других адресов новые попытки."""
        self._attempts.pop(key, None)
        if len(self._trusted) >= MAX_TRUSTED:
            self._trusted.clear()
        self._trusted.add(key)

    def _prune(self) -> None:
        """Забыть неудачи старше паузы: иначе перебор случайных логинов раздувал бы память."""
        now = time.monotonic()
        self._attempts = {
            k: a for k, a in self._attempts.items()
            if a.locked_until > now or now - a.last_at < self.lockout_seconds
        }


def csrf_token(request: Request) -> str:
    """Токен для форм: живёт в сессии, создаётся при первом обращении."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return str(token)


def safe_next(url: str | None) -> str:
    """Куда перейти после входа: только адрес внутри приложения, иначе на главную.

    Без проверки ссылка вида /login?next=https://evil.example вела бы на чужой сайт.
    """
    if url and url.startswith("/") and not url.startswith(("//", "/\\")):
        return url
    return "/"


FLASH_SESSION_KEY = "flash"


def flash(request: Request, message: str) -> None:
    """Сообщение после действия («Лента добавлена»): покажется один раз на следующей странице."""
    request.session[FLASH_SESSION_KEY] = message


def pop_flash(request: Request) -> str | None:
    message = request.session.pop(FLASH_SESSION_KEY, None)
    return str(message) if message else None
