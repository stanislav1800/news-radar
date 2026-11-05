"""Настройки пользователя: скрытые источники, свои вкладки, тема только из своих источников."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Source, User
from tests.conftest import MakeSource
from tests.test_web_stories import Stories, titles
from tests.test_web_topics import follow_search, post


@pytest.fixture
def stories(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> Stories:
    return Stories(session_factory, make_source)


async def source_id(factory: async_sessionmaker[AsyncSession], name: str) -> int:
    async with factory() as session:
        found = await session.scalar(select(Source.id).where(Source.name == name))
        assert found is not None
        return found


async def test_hidden_source_leaves_common_tabs(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await stories.add([("Гомельские ведомости", "Доброе утро, Гомель", timedelta(hours=1))])
    await stories.add([("ТАСС", "Новость ТАСС", timedelta(hours=2))])
    await stories.add([("Гомельские ведомости", "Общая новость", timedelta(hours=3)),
                       ("РБК", "Общая новость", timedelta(hours=3))])
    gomel = await source_id(session_factory, "Гомельские ведомости")

    page = (await logged_in.get("/?view=all")).text
    assert "Не показывать «Гомельские ведомости»" in page
    await post(logged_in, "/settings/hidden-sources", page="/?view=all", source_id=gomel, next="/?view=all")

    # Сюжет только из скрытого источника пропал, общий с другими изданиями — остался
    assert titles((await logged_in.get("/?view=all")).text) == ["Новость ТАСС", "Общая новость"]
    assert "Гомельские ведомости" in (await logged_in.get("/settings")).text

    await post(logged_in, "/settings/hidden-sources", page="/settings", source_id=gomel, action="show")
    assert len(titles((await logged_in.get("/?view=all")).text)) == 3


async def test_user_can_remove_common_tabs(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await stories.add([("ТАСС", "Уголь подорожал", timedelta(hours=1))])
    await follow_search(logged_in, "уголь")
    async with session_factory() as session, session.begin():
        await session.execute(update(User).values(show_top=False, show_all=False))

    page = (await logged_in.get("/?view=all")).text  # убранная вкладка — открываются свои темы

    assert ">Все новости<" not in page and ">Главное<" not in page and "Уголь подорожал" in page


async def test_topic_searches_only_its_sources(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await stories.add([("Хабр", "Вышел Python 3.15", timedelta(hours=1))])
    await stories.add([("ТАСС", "В зоопарке родился питон Python", timedelta(hours=2))])
    topic_id = await follow_search(logged_in, "python")
    habr = await source_id(session_factory, "Хабр")

    await post(logged_in, f"/topics/{topic_id}/sources", page=f"/topics/{topic_id}", source_id=habr)

    assert titles((await logged_in.get(f"/?topic={topic_id}")).text) == ["Вышел Python 3.15"]


async def test_add_to_topic_from_all_news(logged_in: httpx.AsyncClient, stories: Stories) -> None:
    """Во «Всех новостях» у сюжета — свои темы: где его нет, «Добавить в тему», где есть — «Не по теме»."""
    await stories.add([("ТАСС", "Уголь подорожал", timedelta(hours=1))])
    await stories.add([("РБК", "Шахтёры вышли на смену", timedelta(hours=2))])
    await follow_search(logged_in, "уголь")

    page = " ".join((await logged_in.get("/?view=all")).text.split())

    assert "Добавить в тему «уголь»" in page and "Не по теме" in page


async def test_timezone_from_browser_until_chosen_manually(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sito.models import User
    from tests.web import csrf_from

    token = await csrf_from(logged_in, "/settings")

    async def tz() -> str:
        async with session_factory() as session:
            user = await session.scalar(select(User))
            assert user is not None
            return user.timezone

    assert 'data-tz-auto="Europe/Moscow"' in (await logged_in.get("/settings")).text
    auto = {"csrf_token": token, "timezone": "Asia/Novokuznetsk"}
    assert (await logged_in.post("/settings/timezone/auto", data=auto)).status_code == 204
    assert await tz() == "Asia/Novokuznetsk"
    assert (await logged_in.post("/settings/timezone/auto", data={**auto, "timezone": "Mars/Base"})).status_code == 400

    await logged_in.post("/settings/timezone", data={"csrf_token": token, "timezone": "Asia/Omsk"})
    await logged_in.post("/settings/timezone/auto", data=auto)
    assert await tz() == "Asia/Omsk"  # выбран вручную — браузер больше не меняет
    assert "data-tz-auto" not in (await logged_in.get("/settings")).text
