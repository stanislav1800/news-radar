"""Защита веба: ограничение попыток входа, CSRF-токены, безопасные адреса перехода."""

import secrets
import time
from dataclasses import dataclass, field

from starlette.requests import Request

CSRF_SESSION_KEY = "csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class LoginLimiter:
    """Пауза после нескольких неудачных попыток входа для пары «адрес + логин».

    Хранится в памяти процесса: для одного процесса веба этого достаточно,
    после перезапуска счётчики обнуляются.
    """

    max_failures: int
    lockout_seconds: float
    _attempts: dict[tuple[str, str], _Attempts] = field(default_factory=dict)

    def seconds_left(self, key: tuple[str, str]) -> float:
        attempts = self._attempts.get(key)
        return max(0.0, attempts.locked_until - time.monotonic()) if attempts else 0.0

    def record_failure(self, key: tuple[str, str]) -> None:
        attempts = self._attempts.setdefault(key, _Attempts())
        attempts.failures += 1
        if attempts.failures >= self.max_failures:
            attempts.locked_until = time.monotonic() + self.lockout_seconds
            attempts.failures = 0

    def reset(self, key: tuple[str, str]) -> None:
        self._attempts.pop(key, None)


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
