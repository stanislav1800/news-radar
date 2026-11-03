"""Страница «Подключения»: доступ, добавление ленты с пробным сбором, включение, удаление."""

from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, Source, Story
from sito.services.collector.base import FetchResult, RawPost
from sito.services.users import UserService
from tests.conftest import MakeSource
from tests.test_web_stories import Stories
from tests.web import PASSWORD, csrf_from, login


@pytest.fixture
async def admin(client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    async with session_factory() as session, session.begin():
        await UserService(session).create("boss", PASSWORD, is_admin=True)
    assert (await login(client, "boss")).status_code == 303
    return client


@pytest.fixture
def trial_calls(app: FastAPI) -> list[str]:
    """Пробный сбор без сети: адреса со словом broken «не читаются», empty — пустая лента."""
    calls: list[str] = []

    async def fake_trial(url: str) -> FetchResult:
        calls.append(url)
        if "broken" in url:
            raise ValueError("сайт не отвечает")
        if "empty" in url:
            return FetchResult(posts=[])
        return FetchResult(posts=[RawPost(0, "1", url, None, "Добыча угля выросла", None)])

    app.state.trial_fetch = fake_trial
    return calls


async def count_sources(factory: async_sessionmaker[AsyncSession], url: str) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(Source).where(Source.url == url))).scalar_one()


async def add_feed(
    client: httpx.AsyncClient, *, check: bool = True, fields: dict[str, str] | None = None, **overrides: str
) -> httpx.Response:
    token = await csrf_from(client, "/admin/connectors")
    data = {"csrf_token": token, "name": "Уголь Кузбасса", "url": "https://coal.example/rss",
            "poll_interval": "300", **(fields or {}), **overrides}
    if check:
        data["check"] = "on"
    return await client.post("/admin/connectors/1/sources", data=data)


async def test_admin_pages_need_admin(client: httpx.AsyncClient, logged_in: httpx.AsyncClient) -> None:
    assert (await logged_in.get("/admin/connectors")).status_code == 403
    assert "Подключения" not in (await logged_in.get("/")).text  # обычному пользователю ссылки не видно


async def test_admin_pages_need_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/connectors")).status_code == 303


async def test_connectors_page_lists_sources(admin: httpx.AsyncClient, make_source: MakeSource) -> None:
    await make_source(name="ТАСС", url="https://tass.example/rss")

    summary = (await admin.get("/admin/connectors")).text
    assert "1 лента, собирается 1" in " ".join(summary.split()) and "Ленты →" in summary
    html = (await admin.get("/admin/connectors/1")).text  # таблица лент — на странице RSS

    assert "встроенное" in html
    assert "ТАСС" in html and "https://tass.example/rss" in html
    assert "ещё не опрашивался" in html
    assert 'name="url" type="url"' in html  # форма построена по описанию из коллектора


async def test_add_feed_with_trial(
    admin: httpx.AsyncClient, trial_calls: list[str], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await add_feed(admin)

    assert response.status_code == 303
    assert trial_calls == ["https://coal.example/rss"]
    assert await count_sources(session_factory, "https://coal.example/rss") == 1
    page = (await admin.get(response.headers["location"])).text
    assert "Лента «Уголь Кузбасса» добавлена" in page
    assert "Добыча угля выросла" in page
    assert "добавлена" not in (await admin.get("/admin/connectors")).text  # сообщение показывается один раз


@pytest.mark.parametrize(
    ("url", "message"),
    [("https://broken.example/rss", "Лента не читается"), ("https://empty.example/rss", "постов в ней нет")],
)
async def test_failed_trial_does_not_save(
    admin: httpx.AsyncClient, trial_calls: list[str], session_factory: async_sessionmaker[AsyncSession],
    url: str, message: str,
) -> None:
    response = await add_feed(admin, url=url)

    assert response.status_code == 400
    assert message in response.text
    assert "снимите галочку" in response.text
    assert f'value="{url}"' in response.text  # введённое не пропало
    assert await count_sources(session_factory, url) == 0


async def test_add_without_trial(
    admin: httpx.AsyncClient, trial_calls: list[str], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await add_feed(admin, check=False, url="https://broken.example/rss")

    assert response.status_code == 303
    assert trial_calls == []
    assert await count_sources(session_factory, "https://broken.example/rss") == 1


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"url": "не ссылка"}, "Нужна ссылка вида"),
        ({"poll_interval": "10"}, "Не меньше 60"),
        ({"poll_interval": "часто"}, "Нужно целое число"),
        ({"name": "   "}, "Обязательное поле"),
    ],
)
async def test_form_validation(
    admin: httpx.AsyncClient, trial_calls: list[str], fields: dict[str, str], message: str
) -> None:
    response = await add_feed(admin, fields=fields)

    assert response.status_code == 400
    assert message in response.text
    assert trial_calls == []


async def test_duplicate_feed(admin: httpx.AsyncClient, trial_calls: list[str], make_source: MakeSource) -> None:
    await make_source(url="https://coal.example/rss")

    response = await add_feed(admin)

    assert response.status_code == 400
    assert "уже есть" in response.text


async def test_toggle_source_with_htmx(
    admin: httpx.AsyncClient, make_source: MakeSource, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    source_id = await make_source(name="ТАСС")
    token = await csrf_from(admin, "/admin/connectors")

    response = await admin.post(
        f"/admin/sources/{source_id}/enabled", data={"enabled": "false"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.text.lstrip().startswith(f'<tr id="source-{source_id}" class="disabled">')
    assert "Включить" in response.text
    async with session_factory() as session:
        assert (await session.get(Source, source_id)).enabled is False  # type: ignore[union-attr]


async def test_delete_source_cleans_up_stories(
    admin: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    only = await stories.add([("Удаляемый", "Новость только удаляемого", timedelta(hours=2))])
    shared = await stories.add([
        ("Удаляемый", "Ранний пост удаляемого", timedelta(hours=2)),
        ("Остающийся", "Поздний пост остающегося", timedelta(hours=1)),
    ])
    async with session_factory() as session:
        doomed = (await session.execute(select(Source.id).where(Source.name == "Удаляемый"))).scalars().all()
    token = await csrf_from(admin, "/admin/connectors")

    for source_id in doomed:
        assert (await admin.post(f"/admin/sources/{source_id}/delete", data={"csrf_token": token})).status_code == 303

    async with session_factory() as session:
        assert await session.get(Story, only) is None  # опустевший сюжет удалён
        remaining = await session.get(Story, shared)
        assert remaining is not None
        await session.refresh(remaining)
        assert (remaining.posts_count, remaining.sources_count) == (1, 1)
        assert (await session.execute(select(func.count()).select_from(Post))).scalar_one() == 1


async def test_source_name_cannot_break_out_of_js(admin: httpx.AsyncClient, make_source: MakeSource) -> None:
    await make_source(name="Лента'); alert(1); ('</script><script>alert(2)</script>")

    html = (await admin.get("/admin/connectors")).text

    assert "<script>alert(2)</script>" not in html
    assert "'); alert(1)" not in html


async def test_admin_actions_require_csrf(admin: httpx.AsyncClient, make_source: MakeSource) -> None:
    source_id = await make_source()

    assert (await admin.post(f"/admin/sources/{source_id}/delete")).status_code == 403
    assert (await admin.post(f"/admin/sources/{source_id}/enabled", data={"enabled": "false"})).status_code == 403
