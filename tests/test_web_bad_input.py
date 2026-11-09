"""Кривые адреса и формы: понятный ответ (404, 400 или обычная страница), а не ошибка 500."""

import httpx
import pytest

from tests.web import csrf_from

HUGE = "9" * 30


@pytest.mark.parametrize("path", [
    f"/?cursor={HUGE}-1",
    f"/?cursor=1-{HUGE}",
    f"/feed/items?cursor={HUGE}-{HUGE}",
    f"/feed/newer?since={HUGE}",
    "/feed/newer?since=-99999999999999999",
    f"/?topic={HUGE}",  # несуществующая тема — обычная лента
])
async def test_bad_cursor_is_ordinary_page(logged_in: httpx.AsyncClient, path: str) -> None:
    assert (await logged_in.get(path)).status_code == 200


@pytest.mark.parametrize("path", [f"/stories/{HUGE}", f"/topics/{HUGE}"])
async def test_huge_id_is_not_found(logged_in: httpx.AsyncClient, path: str) -> None:
    assert (await logged_in.get(path)).status_code == 404


async def test_hide_missing_source(logged_in: httpx.AsyncClient) -> None:
    token = await csrf_from(logged_in, "/settings")
    for source_id in ("987654", HUGE):
        response = await logged_in.post(
            "/settings/hidden-sources", data={"source_id": source_id, "csrf_token": token},
        )
        assert response.status_code == 404
