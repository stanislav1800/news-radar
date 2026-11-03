"""Время для людей: в часовом поясе пользователя, относительно «сейчас»."""

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from markupsafe import Markup, escape

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


MONTHS_GENITIVE = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                   "сентября", "октября", "ноября", "декабря"]


def day_label(moment: datetime, timezone: str, now: datetime | None = None) -> str:
    """Заголовок дня в ленте: «Сегодня», «Вчера», «28 октября», «28 октября 2025»."""
    zone = _zone(timezone)
    day = moment.astimezone(zone).date()
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    if day == today:
        return "Сегодня"
    if (today - day).days == 1:
        return "Вчера"
    label = f"{day.day} {MONTHS_GENITIVE[day.month - 1]}"
    return label if day.year == today.year else f"{label} {day.year}"


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


def compact(value: int | None) -> str:
    """Число подписчиков коротко: 950, 12 тыс., 1,2 млн."""
    if value is None:
        return ""
    if value < 10_000:
        return f"{value:,}".replace(",", " ")
    if value < 1_000_000:
        return f"{round(value / 1000)} тыс."
    return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"


_REF = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str, story_ids: Sequence[int]) -> str:
    html = str(escape(text))
    html = _BOLD.sub(r"<strong>\1</strong>", html)

    def ref(match: re.Match[str]) -> str:
        links = []
        for raw in match.group(1).split(","):
            n = int(raw)
            if 1 <= n <= len(story_ids):
                links.append(f'<a href="/stories/{story_ids[n - 1]}">{n}</a>')
        return f'<sup class="ref">{", ".join(links)}</sup>' if links else ""

    return _REF.sub(ref, html)


def report_html(text: str, story_ids: Sequence[int]) -> Markup:
    """Ответ LLM (markdown: ## заголовки, списки, **жирный**, [n] — сюжеты) в безопасный HTML."""
    out: list[str] = []
    in_list = False
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        item = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", line)
        if in_list and not item:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if heading := re.match(r"^#{1,6}\s+(.*)", line):
            out.append(f"<h3>{_inline(heading.group(1).strip('* '), story_ids)}</h3>")
        elif item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(item.group(1), story_ids)}</li>")
        else:
            out.append(f"<p>{_inline(line, story_ids)}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("".join(out))
