"""Общая лента и страница сюжета."""

import re
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.config import settings
from sito.models import Post, PostStatus
from sito.models.post import EMBEDDING_DIM
from sito.repositories.stories import StoryRepository
from sito.utils.lemmas import lemma_text
from tests.conftest import MakeSource

NOW = datetime.now(UTC)


def vec(axis: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    return v


class Stories:
    """Создаёт сюжеты с постами напрямую, без пайплайна."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> None:
        self.factory = factory
        self.make_source = make_source
        self.n = 0

    async def add(
        self, posts: list[tuple[str, str, timedelta]], embedding: list[float] | None = None,
        url: str | None = None, text: str | None = "Подробности по ссылке.",
    ) -> int:
        """posts: (имя источника, заголовок, сколько времени назад). text=None — пост из одного заголовка."""
        async with self.factory() as session, session.begin():
            repo = StoryRepository(session)
            first_at = NOW - max(ago for _, _, ago in posts)
            story = await repo.create(posts[0][1], first_at)
            for source_name, title, ago in posts:
                self.n += 1
                source_id = await self.make_source(name=source_name)
                await session.execute(insert(Post).values(
                    source_id=source_id, external_id=f"id-{self.n}", url=url or f"https://news.example/{self.n}",
                    url_normalized=f"https://news.example/{self.n}", title=title, text=text, content_hash=f"h{self.n}",
                    title_lemmas=lemma_text(title), text_lemmas=lemma_text(text),
                    published_at=NOW - ago, status=PostStatus.CLUSTERED, story_id=story.id,
                    embedding=embedding or vec(self.n % EMBEDDING_DIM),
                ))
            await repo.refresh(story.id)
            return story.id


def titles(html: str) -> list[str]:
    return re.findall(r'class="story-title" href="/stories/\d+">([^<]+)</a>', html)


async def test_feed_requires_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/feed/items")).status_code == 303
    assert (await client.get("/stories/1")).status_code == 303


async def test_feed_newest_first_with_counts(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    await stories.add([("ТАСС", "Старый сюжет", timedelta(hours=5))])
    await stories.add([
        ("ТАСС", "Столкнулись поезда", timedelta(hours=2)),
        ("РБК", "40 погибших при столкновении поездов", timedelta(hours=1)),
    ])

    html = (await logged_in.get("/?view=all")).text

    assert titles(html) == ["40 погибших при столкновении поездов", "Старый сюжет"]
    assert "2 издания" in html
    assert "обновлено 1 ч назад" in html


async def test_top_tab_shows_widely_covered_stories(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    await stories.add([(f"Издание {n}", "Подхватили все", timedelta(hours=1)) for n in range(5)])
    await stories.add([("ТАСС", "Одно издание", timedelta(hours=1))])

    assert titles((await logged_in.get("/?view=top")).text) == ["Подхватили все"]
    assert titles((await logged_in.get("/")).text) == ["Подхватили все"]  # без тем открывается «Главное»

async def test_feed_pagination(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    make_source: MakeSource, app: object,
) -> None:
    stories = Stories(session_factory, make_source)
    page_size = settings.feed_page_size
    for i in range(page_size + 3):
        await stories.add([("ТАСС", f"Сюжет {i:02d}", timedelta(minutes=i + 1))])

    first = (await logged_in.get("/?view=all")).text
    assert len(titles(first)) == page_size
    cursor = re.search(r'hx-get="/feed/items\?view=all&(?:amp;)?cursor=([^"]+)"', first)
    assert cursor, "нет ссылки «Показать ещё»"

    more = await logged_in.get(f"/feed/items?view=all&cursor={cursor.group(1)}", headers={"HX-Request": "true"})

    rest = titles(more.text)
    assert rest == [f"Сюжет {i:02d}" for i in range(page_size, page_size + 3)]
    assert "Показать ещё" not in more.text
    assert "<html" not in more.text  # для HTMX только карточки, без страницы целиком


async def test_broken_cursor_shows_first_page(logged_in: httpx.AsyncClient) -> None:
    assert (await logged_in.get("/?cursor=garbage")).status_code == 200


async def test_story_page(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    story_id = await stories.add([
        ("ТАСС", "В Испании столкнулись поезда", timedelta(hours=3)),
        ("Интерфакс", "Предварительно 40 пострадавших", timedelta(hours=2)),
        ("РБК", "Пострадали 120 человек, 40 погибли", timedelta(hours=1)),
    ])

    html = (await logged_in.get(f"/stories/{story_id}")).text

    assert "<h1>Пострадали 120 человек, 40 погибли</h1>" in html  # заголовок по свежему посту
    assert "3 издания" in html and "3 поста" in html
    # Хронология: свежее сверху, первый источник помечен
    order = [html.index(t) for t in ("Пострадали 120", "Предварительно 40", "В Испании столкнулись")]
    assert order == sorted(order)
    assert re.search(r'<span class="source">ТАСС</span>\s*<span class="badge">первым</span>', html)
    assert 'target="_blank" rel="noopener noreferrer"' in html


async def test_story_page_shows_similar(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    main = await stories.add([("ТАСС", "Столкнулись поезда", timedelta(hours=2))], embedding=vec(0))
    related = [0.0] * EMBEDDING_DIM
    related[0], related[1] = 0.9, (1 - 0.9**2) ** 0.5  # сходство 0,9 с главным
    await stories.add([("РБК", "Премьер потрясён катастрофой", timedelta(hours=1))], embedding=related)
    await stories.add([("Хабр", "Вышел FastAPI 1.0", timedelta(hours=1))], embedding=vec(5))

    html = (await logged_in.get(f"/stories/{main}")).text

    assert "Похожие" in html
    assert "Премьер потрясён катастрофой" in html
    assert "Вышел FastAPI 1.0" not in html


async def test_unknown_story(logged_in: httpx.AsyncClient) -> None:
    assert (await logged_in.get("/stories/999999")).status_code == 404


async def test_dangerous_original_link_is_neutralized(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    story_id = await stories.add([("Чужая лента", "Обычный заголовок", timedelta(hours=1))], url="javascript:alert(1)")

    html = (await logged_in.get(f"/stories/{story_id}")).text

    assert "javascript:" not in html
    assert 'href="#"' in html


async def test_titles_are_escaped(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    stories = Stories(session_factory, make_source)
    await stories.add([("ТАСС", "<script>alert(1)</script>", timedelta(hours=1))])

    html = (await logged_in.get("/?view=all")).text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


async def test_new_stories_arrive_without_reload(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """Лента раз в минуту спрашивает новые сюжеты и получает сами карточки, а не кнопку «показать»."""
    stories = Stories(session_factory, make_source)
    await stories.add([("ТАСС", "Старый сюжет", timedelta(hours=2))])
    page = (await logged_in.get("/?view=all")).text
    since = re.search(r'since=(\d+)', page)
    assert since and 'id="fresh"' in page
    assert (await logged_in.get(f"/feed/newer?view=all&since={since.group(1)}")).text == ""

    await stories.add([("РБК", "Свежий сюжет", timedelta(minutes=1))])
    response = (await logged_in.get(f"/feed/newer?view=all&since={since.group(1)}")).text

    assert titles(response) == ["Свежий сюжет"]
    assert 'hx-swap-oob="true"' in response  # опросчик продолжает с новой отметки


async def test_feed_remembers_last_tab_and_story_links_back(
    logged_in: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    story = await Stories(session_factory, make_source).add([("ТАСС", "Сюжет", timedelta(hours=1))])
    await logged_in.get("/?view=all")

    assert 'class="tab active" href="/?view=all"' in (await logged_in.get("/")).text  # главная — та же вкладка
    assert 'href="/?view=all#story-' + str(story) + '" data-back' in (await logged_in.get(f"/stories/{story}")).text
