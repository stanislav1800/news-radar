"""Время для людей: в часовом поясе пользователя, относительно «сейчас»."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_time(moment: datetime, timezone: str, now: datetime | None = None) -> str:
    """«14:05», «вчера, 14:05», «28 окт, 14:05», «28 окт 2025, 14:05»."""
    zone = _zone(timezone)
    local = moment.astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    clock = f"{local:%H:%M}"
    if local.date() == today:
        return clock
    if (today - local.date()).days == 1:
        return f"вчера, {clock}"
    day = f"{local.day} {MONTHS[local.month - 1]}"
    if local.year != today.year:
        day += f" {local.year}"
    return f"{day}, {clock}"


def ago(moment: datetime, timezone: str, now: datetime | None = None) -> str:
    """«только что», «5 мин назад», «3 ч назад», дальше — как local_time."""
    current = now or datetime.now(UTC)
    minutes = int((current - moment).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 12 * 60:
        return f"{minutes // 60} ч назад"
    return local_time(moment, timezone, current)


def plural(count: int, one: str, few: str, many: str) -> str:
    """1 издание, 2 издания, 5 изданий, 21 издание."""
    n = abs(count) % 100
    if 11 <= n <= 14:
        word = many
    elif n % 10 == 1:
        word = one
    elif 2 <= n % 10 <= 4:
        word = few
    else:
        word = many
    return f"{count} {word}"


def safe_url(url: str) -> str:
    """Ссылка на оригинал из чужой ленты: только http и https.

    Иначе лента могла бы подсунуть javascript:... и выполнить код в браузере пользователя.
    """
    return url if url.lower().startswith(("http://", "https://")) else "#"
