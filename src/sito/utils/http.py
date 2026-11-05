"""Загрузка по HTTP с ограничениями: чужой сервер не должен положить worker.

httpx с `timeout=15` ограничивает каждую операцию отдельно, а не загрузку целиком: сервер, отдающий тело
по байту, держит её сколько угодно. А `client.get` читает тело целиком — ссылка на видеофайл в сотни
мегабайт оказалась бы в памяти.
"""

import asyncio
from collections.abc import Callable

import httpx


class ResponseTooLarge(httpx.HTTPError):
    """Ответ больше допустимого: дальше не читаем."""


async def get_limited(
    client: httpx.AsyncClient, url: str, *, max_bytes: int, timeout: float,
    headers: dict[str, str] | None = None, accept: Callable[[httpx.Headers], bool] | None = None,
) -> tuple[httpx.Response, bytes | None]:
    """GET с потолком размера и общего времени. accept(заголовки) → False — тело не читать (вернётся None).

    Ответ возвращается уже закрытым; ошибки HTTP (4xx/5xx) не бросаются — их проверяет вызывающий.
    """
    async with asyncio.timeout(timeout), client.stream("GET", url, headers=headers) as response:
        if accept is not None and response.is_success and not accept(response.headers):
            return response, None
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
        return response, b"".join(chunks)
