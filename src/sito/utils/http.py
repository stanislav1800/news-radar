"""Загрузка по HTTP с ограничениями: чужой сервер не должен положить worker.

httpx с `timeout=15` ограничивает каждую операцию отдельно, а не загрузку целиком: сервер, отдающий тело
по байту, держит её сколько угодно. А `client.get` читает тело целиком — ссылка на видеофайл в сотни
мегабайт оказалась бы в памяти.

Ссылки на статьи присылают авторы чужих каналов и лент: с `resolve` загрузка идёт только на публичные адреса
(не в docker-сеть, не в локальную сеть, не на служебные адреса облака), и каждый редирект проверяется заново.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

MAX_REDIRECTS = 5
# Имя хоста → его адреса (как getaddrinfo); подменяется в тестах, чтобы не ходить в DNS
Resolve = Callable[[str, int], Awaitable[list[str]]]


class ResponseTooLarge(httpx.HTTPError):
    """Ответ больше допустимого: дальше не читаем."""


class UnsafeUrl(httpx.HTTPError):
    """Адрес не публичный (внутренняя сеть, localhost, служебный) или не http(s) на стандартном порту."""


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_public(url: str, resolve: Resolve = resolve_host) -> None:
    """UnsafeUrl, если ссылка ведёт не в интернет. Повторный резолв при подключении (DNS rebinding) не ловит."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrl(f"не http(s): {url}")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrl(f"кривой порт: {url}") from exc
    if port not in (80, 443):
        raise UnsafeUrl(f"нестандартный порт {port}: {url}")
    try:
        addresses = await resolve(parts.hostname, port)
    except OSError as exc:
        raise UnsafeUrl(f"хост {parts.hostname} не найден") from exc
    if not addresses:
        raise UnsafeUrl(f"хост {parts.hostname} не найден")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeUrl(f"{parts.hostname} ведёт во внутреннюю сеть ({ip})")


async def get_limited(
    client: httpx.AsyncClient, url: str, *, max_bytes: int, timeout: float,
    headers: dict[str, str] | None = None, accept: Callable[[httpx.Headers], bool] | None = None,
    resolve: Resolve | None = None,
) -> tuple[httpx.Response, bytes | None]:
    """GET с потолком размера и общего времени. accept(заголовки) → False — тело не читать (вернётся None).

    С resolve — только публичные адреса, редиректы проверяются по одному (UnsafeUrl). Ответ возвращается
    уже закрытым; ошибки HTTP (4xx/5xx) не бросаются — их проверяет вызывающий.
    """
    async with asyncio.timeout(timeout):
        if resolve is None:
            async with client.stream("GET", url, headers=headers) as response:
                return response, await _read(response, max_bytes=max_bytes, accept=accept)
        for _ in range(MAX_REDIRECTS + 1):
            await check_public(url, resolve)
            async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                if not response.is_redirect:
                    return response, await _read(response, max_bytes=max_bytes, accept=accept)
                url = str(response.url.join(response.headers["location"]))
        raise httpx.TooManyRedirects(f"больше {MAX_REDIRECTS} редиректов", request=response.request)


async def _read(
    response: httpx.Response, *, max_bytes: int, accept: Callable[[httpx.Headers], bool] | None,
) -> bytes | None:
    if accept is not None and response.is_success and not accept(response.headers):
        return None
    length = response.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes:
        raise ResponseTooLarge(f"ответ {int(length) // 1_000_000} МБ — больше {max_bytes // 1_000_000} МБ")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLarge(f"ответ больше {max_bytes // 1_000_000} МБ")
        chunks.append(chunk)
    return b"".join(chunks)
