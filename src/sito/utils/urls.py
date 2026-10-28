import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "yclid", "ysclid", "mc_cid", "mc_eid",
    "from", "ref", "source", "_openstat", "rss", "erid",
})
TRACKING_PREFIXES = ("utm_",)

DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(param: str) -> bool:
    name = param.lower()
    return name in TRACKING_PARAMS or name.startswith(TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Приводит адрес статьи к одному виду, чтобы находить дубли.

    Регистр пути не меняется: на многих сайтах /News и /news — разные страницы.
    """
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"Не абсолютный URL: {url!r}")

    scheme = "https" if parts.scheme.lower() in DEFAULT_PORTS else parts.scheme.lower()

    host = parts.hostname.removeprefix("www.")
    if parts.port and parts.port != DEFAULT_PORTS.get(parts.scheme.lower()):
        host = f"{host}:{parts.port}"

    path = parts.path.rstrip("/")

    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ))

    return urlunsplit((scheme, host, path, query, ""))


def content_hash(title: str, text: str | None) -> str:
    """Отпечаток содержимого: изменился — значит, пост отредактировали."""
    parts = (" ".join(s.split()) for s in (title, text or ""))
    data = "\x00".join(parts)
    return hashlib.sha256(data.encode()).hexdigest()
