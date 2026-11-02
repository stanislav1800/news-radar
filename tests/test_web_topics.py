"""Веб тем: поиск, «Следить», моя лента, 👍/👎, страница темы, RSS."""

import re
from datetime import timedelta
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Topic
from sito.services.users import UserService
from tests.conftest import FakeEmbedder, MakeSource
from tests.test_topics import direction
from tests.test_web_stories import Stories, titles
from tests.web import PASSWORD, csrf_from, login


@pytest.fixture
def stories(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> Stories:
    return Stories(session_factory, make_source)


async def topics_of(factory: async_sessionmaker[AsyncSession]) -> list[Topic]:
    async with factory() as session:
        return list(await session.scalars(select(Topic).order_by(Topic.id)))


async def post(client: httpx.AsyncClient, path: str, page: str = "/search", **data: object) -> httpx.Response:
    token = await csrf_from(client, page)
    return await client.post(path, data={"csrf_token": token, **data})


async def follow_search(client: httpx.AsyncClient, query: str) -> int:
    response = await post(client, "/topics", query=query)
    assert response.status_code == 303
    match = re.fullmatch(r"/\?topic=(\d+)", response.headers["location"])
    assert match
    return int(match.group(1))


async def test_pages_need_login(client: httpx.AsyncClient) -> None:
    for path in ("/", "/search?q=x", "/topics/1"):
        assert (await client.get(path)).status_code == 303, path


async def test_search_one_list_words_then_meaning(
    logged_in: httpx.AsyncClient, stories: Stories, embedder: FakeEmbedder
) -> None:
    """Обычный текст: сначала сюжеты, где совпало больше слов (в любой форме), ниже — по смыслу."""
    await stories.add([("РБК", "Министр о добыче", timedelta(hours=2))], embedding=direction(1), text=None)
    await stories.add([("ТАСС", "Добыча угля в Кузбассе выросла", timedelta(hours=3))], embedding=direction(80))
    await stories.add([("РБК", "Шахтёры перевыполнили план", timedelta(hours=2))], embedding=direction(5))
    await stories.add([("РБК", "Футбол", timedelta(hours=2))], embedding=direction(90, base=3, other=4))
    # Голый заголовок без текста: его вектор похож на любой короткий запрос, по смыслу не ищется
    await stories.add([("Известия", "Губернатор о главном", timedelta(hours=2))], embedding=direction(2), text=None)
    embedder.vectors["добыча угля в кузбассе"] = direction(0)

    html = (await logged_in.get("/search", params={"q": "  добыча   угля в кузбассе "})).text

    assert titles(html) == ["Добыча угля в Кузбассе выросла", "Министр о добыче", "Шахтёры перевыполнили план"]
    assert html.count("по смыслу</span>") == 1  # пометка только у найденного по смыслу
    assert embedder.calls == ["добыча угля в кузбассе"]  # пробелы схлопнуты
    assert "по запросу «добыча угля в кузбассе»" in html and ">Следить</button>" in html

    await logged_in.get("/search", params={"q": 'добыча or "уголь" -нефть'})
    assert embedder.calls[-1] == "добыча уголь"  # синтаксис поиска по словам модели не передаётся


async def test_rare_words_rank_higher(logged_in: httpx.AsyncClient, stories: Stories) -> None:
    """«Уголь» встречается реже «Кузбасса»: сюжет про уголь выше сюжета про погоду в Кузбассе."""
    for n in range(4):
        await stories.add([("ТАСС", f"Кузбасс: новость {n}", timedelta(hours=5))])
    await stories.add([("ТАСС", "Погода в Кузбассе", timedelta(hours=1))])
    await stories.add([("ТАСС", "Цены на уголь", timedelta(hours=2))])

    html = (await logged_in.get("/search", params={"q": "уголь в кузбассе"})).text

    assert titles(html)[0] == "Цены на уголь"


async def test_follow_with_examples_from_results(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession],
    embedder: FakeEmbedder,
) -> None:
    """👍/👎 в результатах поиска уходят в тему первыми примерами вместе с «Следить»."""
    from sito.models import TopicExample

    good = await stories.add([("ТАСС", "Шахтёры перевыполнили план", timedelta(hours=2))], embedding=direction(0))
    bad = await stories.add([("ТАСС", "Угольный утюг: обзор", timedelta(hours=2))], embedding=direction(90))
    embedder.vectors["уголь"] = direction(0)

    response = await post(logged_in, "/topics", query="уголь", like=str(good), dislike=str(bad))

    assert response.status_code == 303
    async with session_factory() as session:
        votes = {(e.story_id, e.liked) for e in await session.scalars(select(TopicExample))}
    assert votes == {(good, True), (bad, False)}
    my = (await logged_in.get("/?view=my")).text
    assert "Шахтёры перевыполнили план" in titles(my) and "Угольный утюг: обзор" not in titles(my)


async def test_search_works_without_model(logged_in: httpx.AsyncClient, stories: Stories, app: FastAPI) -> None:
    class Broken:
        name, dim = "broken", 768

        async def embed(self, texts: object) -> list[list[float]]:
            raise RuntimeError("модель не загрузилась")

    app.state.embedder = Broken()
    await stories.add([("ТАСС", "Уголь подорожал", timedelta(hours=1))])

    html = (await logged_in.get("/search", params={"q": "уголь"})).text

    assert titles(html) == ["Уголь подорожал"]
    assert "Поиск по смыслу сейчас недоступен" in html


async def test_follow_search_and_personal_feed(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await stories.add([("ТАСС", "Уголь подорожал", timedelta(hours=1))])
    await stories.add([("ТАСС", "Футбол", timedelta(hours=1))])

    topic_id = await follow_search(logged_in, "уголь")

    page = await logged_in.get(f"/topics/{topic_id}")
    assert "Тема «уголь» создана. Сейчас в ней сюжетов: 1" in page.text
    my = (await logged_in.get("/?view=my")).text
    assert titles(my) == ["Уголь подорожал"]
    assert "есть слова: уголь" in my and "Не по теме" in my
    [topic] = await topics_of(session_factory)
    assert topic.name == "уголь" and topic.query == "уголь"


async def test_empty_personal_feed_explains_topics(logged_in: httpx.AsyncClient) -> None:
    html = (await logged_in.get("/")).text
    assert "О чём вы хотите знать?" in html


async def test_vote_with_htmx_returns_fragment(
    logged_in: httpx.AsyncClient, stories: Stories
) -> None:
    good = await stories.add([("ТАСС", "Уголь: добыча выросла", timedelta(hours=2))], embedding=direction(0))
    bad = await stories.add([("ТАСС", "Уголь для шашлыка", timedelta(hours=1))], embedding=direction(90))
    topic_id = await follow_search(logged_in, "уголь")
    token = await csrf_from(logged_in, "/")

    response = await logged_in.post(
        "/topics/vote",
        data={"topic_id": topic_id, "story_id": bad, "vote": "dislike", "next": "/"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Убрано из темы" in response.text and "Отменить" in response.text
    assert 'class="reason out"' in response.text
    assert titles((await logged_in.get("/?view=my")).text) == ["Уголь: добыча выросла"]

    # 👍 без HTMX — обычная форма: переход обратно
    response = await post(logged_in, "/topics/vote", page="/", topic_id=topic_id, story_id=good,
                          vote="like", next=f"/stories/{good}")
    assert response.status_code == 303 and response.headers["location"] == f"/stories/{good}"
    assert "Вы добавили этот сюжет" in (await logged_in.get(f"/stories/{good}")).text


async def test_vote_redirect_stays_inside_app(logged_in: httpx.AsyncClient, stories: Stories) -> None:
    story = await stories.add([("ТАСС", "Уголь", timedelta(hours=1))])
    topic_id = await follow_search(logged_in, "уголь")

    response = await post(logged_in, "/topics/vote", topic_id=topic_id, story_id=story, vote="like",
                          next="https://evil.example/")

    assert response.headers["location"] == "/"


async def test_follow_story_from_story_page(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    story = await stories.add([("ТАСС", "Запуск ракеты Ангара", timedelta(hours=1))], embedding=direction(0))
    page = (await logged_in.get(f"/stories/{story}")).text
    assert "Следить за похожими" in page

    response = await post(logged_in, "/topics/follow", page=f"/stories/{story}", story_id=story)

    assert response.status_code == 303
    [topic] = await topics_of(session_factory)
    assert topic.name == "Запуск ракеты Ангара" and topic.query is None
    topic_feed = (await logged_in.get(response.headers["location"])).text
    assert titles(topic_feed) == ["Запуск ракеты Ангара"]


async def test_topic_page_suggestions(
    logged_in: httpx.AsyncClient, stories: Stories, embedder: FakeEmbedder
) -> None:
    await stories.add([("ТАСС", "Уголь подорожал", timedelta(hours=2))], embedding=direction(80))
    await stories.add([("РБК", "Шахтёры вышли на смену", timedelta(hours=1))], embedding=direction(5))
    embedder.vectors["уголь"] = direction(0)
    topic_id = await follow_search(logged_in, "уголь")

    html = (await logged_in.get(f"/?topic={topic_id}")).text
    suggestions = html.split("Ещё может подойти")[1]

    assert titles(suggestions) == ["Шахтёры вышли на смену"]
    assert "Не подходит" in suggestions


async def test_topic_feed_asks_about_borderline_stories(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Сюжет чуть ниже порога темы спрашивается «Подходит?» прямо в ленте, после третьей карточки."""
    first = await stories.add([("ТАСС", "Пожар 1", timedelta(hours=1))], embedding=direction(0))
    for n in range(2, 6):
        await stories.add([("ТАСС", f"Пожар {n}", timedelta(hours=n))], embedding=direction(n))
    await stories.add([("РБК", "Спорный", timedelta(hours=30))], embedding=direction(32))  # 0.848
    await post(logged_in, "/topics/follow", page=f"/stories/{first}", story_id=first)
    [topic] = await topics_of(session_factory)

    html = (await logged_in.get(f"/?topic={topic.id}")).text

    assert "Подходит к теме" in html
    assert titles(html)[:5] == ["Пожар 1", "Пожар 2", "Пожар 3", "Спорный", "Пожар 4"]


async def test_someone_elses_topic_is_not_found(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    story = await stories.add([("ТАСС", "Уголь", timedelta(hours=1))])
    topic_id = await follow_search(logged_in, "уголь")
    async with session_factory() as session, session.begin():
        await UserService(session).create("petr", PASSWORD)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as other:
        await login(other, "petr")
        assert (await other.get(f"/topics/{topic_id}")).status_code == 404
        assert (await post(other, f"/topics/{topic_id}/delete", page="/")).status_code == 404
        await post(other, "/topics/vote", page="/", topic_id=topic_id, story_id=story, vote="dislike")
        assert titles((await other.get(f"/?topic={topic_id}")).text) == []

    assert len(await topics_of(session_factory)) == 1
    assert titles((await logged_in.get("/?view=my")).text) == ["Уголь"]  # чужой 👎 не сработал


async def test_rename_pause_delete(
    logged_in: httpx.AsyncClient, stories: Stories, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await stories.add([("ТАСС", "Уголь подорожал", timedelta(hours=1))])
    topic_id = await follow_search(logged_in, "уголь")
    page = f"/topics/{topic_id}"

    response = await post(logged_in, page, page=page, name="", query="уголь")
    assert response.status_code == 400 and "Обязательное поле" in response.text

    await post(logged_in, page, page=page, name="Угольная отрасль", query="уголь or шахта")
    [topic] = await topics_of(session_factory)
    assert topic.name == "Угольная отрасль" and topic.query == "уголь or шахта"

    await post(logged_in, f"{page}/pause", page=page, paused="true")
    assert titles((await logged_in.get("/?view=my")).text) == []
    assert "tab paused" in (await logged_in.get("/")).text  # вкладка темы на паузе приглушена
    await post(logged_in, f"{page}/pause", page=page, paused="false")
    assert titles((await logged_in.get("/?view=my")).text) == ["Уголь подорожал"]

    await post(logged_in, f"{page}/delete", page=page)
    assert await topics_of(session_factory) == []


async def test_rss_feed(
    app: FastAPI, logged_in: httpx.AsyncClient, stories: Stories,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await stories.add([
        ("ТАСС", "Уголь подешевел", timedelta(hours=2)),
        ("РБК", "Уголь & <b>подорожал</b>", timedelta(hours=1)),  # спецсимволы XML должны экранироваться
    ])
    topic_id = await follow_search(logged_in, "уголь")
    [topic] = await topics_of(session_factory)
    rss_path = f"/rss/{topic.rss_token}"
    assert rss_path in (await logged_in.get(f"/topics/{topic_id}")).text

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anonymous:
        response = await anonymous.get(rss_path)  # RSS-читалка входит без пароля
        assert (await anonymous.get("/rss/wrong-token")).status_code == 404

    assert response.headers["content-type"].startswith("application/rss+xml")
    channel = ET.fromstring(response.text).find("channel")
    assert channel is not None
    [item] = channel.findall("item")
    assert item.findtext("title") == "Уголь & <b>подорожал</b>"
    assert item.findtext("link", "").startswith("http://testserver/stories/")
    assert "Изданий: 2, первым — ТАСС" in item.findtext("description", "")

    # Новый адрес — старый перестаёт работать
    await post(logged_in, f"/topics/{topic_id}/rss-token", page=f"/topics/{topic_id}")
    assert (await logged_in.get(rss_path)).status_code == 404


async def test_vote_requires_csrf(logged_in: httpx.AsyncClient, stories: Stories) -> None:
    story = await stories.add([("ТАСС", "Уголь", timedelta(hours=1))])
    topic_id = await follow_search(logged_in, "уголь")

    response = await logged_in.post("/topics/vote", data={"topic_id": topic_id, "story_id": story, "vote": "dislike"})

    assert response.status_code == 403
    assert titles((await logged_in.get("/?view=my")).text) == ["Уголь"]


async def test_story_page_timeline_and_follow_in_place(logged_in: httpx.AsyncClient, stories: Stories) -> None:
    story = await stories.add([("ТАСС", "Запуск ракеты", timedelta(hours=1))], embedding=direction(0),
                              text="Ракета стартовала с Восточного.\nВторой абзац не показываем.")
    page = (await logged_in.get(f"/stories/{story}")).text
    assert "Ракета стартовала с Восточного." in page and "Второй абзац" not in page
    assert "Читать в источнике" in page

    token = await csrf_from(logged_in, f"/stories/{story}")
    response = await logged_in.post("/topics/follow", data={"story_id": str(story)},
                                    headers={"HX-Request": "true", "X-CSRF-Token": token})

    assert "Создана тема" in response.text and "Отменить" in response.text and "<html" not in response.text


async def test_word_topic_asks_about_stories_like_its_matches(
    logged_in: httpx.AsyncClient, stories: Stories, embedder: FakeEmbedder
) -> None:
    """Тема по словам без примеров спрашивает о сюжетах, похожих на найденное по словам, но сама их не берёт."""
    for n in range(1, 5):
        await stories.add([("ТАСС", f"ДТП на трассе {n}", timedelta(hours=n))], embedding=direction(n))
    await stories.add([("РБК", "Лобовое столкновение", timedelta(hours=30))], embedding=direction(25))  # 0.91
    embedder.vectors["дтп"] = direction(0)
    topic_id = await follow_search(logged_in, "дтп")

    html = (await logged_in.get(f"/?topic={topic_id}")).text

    assert "Подходит к теме" in html
    assert titles(html) == ["ДТП на трассе 1", "ДТП на трассе 2", "ДТП на трассе 3", "Лобовое столкновение", "ДТП на трассе 4"]


async def test_search_shows_what_topic_would_find(logged_in: httpx.AsyncClient, stories: Stories) -> None:
    """Перед «Следить» видно, сколько сюжетов со всеми словами есть, и если ни одного — предупреждение."""
    await stories.add([("ТАСС", "Добыча угля в Кузбассе", timedelta(hours=1))])

    found = (await logged_in.get("/search", params={"q": "уголь кузбасс"})).text
    empty = (await logged_in.get("/search", params={"q": "уголь кузбасс экспорт"})).text

    assert "за 30 дней таких 1" in " ".join(found.split())
    assert "все эти слова, за 30 дней нет" in " ".join(empty.split())
