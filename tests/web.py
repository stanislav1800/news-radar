"""Помощники для тестов веба: вход и CSRF-токен из формы."""

import re

import httpx

PASSWORD = "correct-horse-battery"


async def csrf_from(client: httpx.AsyncClient, path: str = "/login") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', (await client.get(path)).text)
    assert match, "в форме нет CSRF-токена"
    return match.group(1)


async def login(client: httpx.AsyncClient, username: str, password: str = PASSWORD, **extra: str) -> httpx.Response:
    token = await csrf_from(client)
    return await client.post(
        "/login", data={"username": username, "password": password, "csrf_token": token, **extra}
    )
