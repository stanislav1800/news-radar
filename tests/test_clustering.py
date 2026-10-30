"""Склейка в сюжеты. Векторы задаются вручную, чтобы точно выставить сходство между постами."""

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus, Story
from sito.models.post import EMBEDDING_DIM
from sito.repositories.stories import StoryRepository
from sito.services.clustering import ClusteringService
from tests.conftest import MakeSource

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def vec(similarity: float = 1.0, axis: int = 1) -> list[float]:
    """Единичный вектор со сходством similarity к базовому вектору vec()."""
    v = [0.0] * EMBEDDING_DIM
    v[0] = similarity
    v[axis] = math.sqrt(max(0.0, 1 - similarity**2))
    return v


class Posts:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.n = 0

    async def add(
        self, source_id: int, embedding: list[float], at: datetime = T0, title: str | None = None,
        text: str | None = None, story_id: int | None = None, links: list[str] | None = None,
    ) -> int:
        self.n += 1
        async with self.factory() as session, session.begin():
            return (await session.execute(insert(Post).values(
                source_id=source_id, external_id=f"id-{self.n}", url=f"https://news.example/{self.n}",
                url_normalized=f"https://news.example/{self.n}", title=title or f"Пост {self.n}", text=text,
                links=links or [],
                content_hash=f"h{self.n}", published_at=at, embedding=embedding,
                status=PostStatus.EMBEDDED, story_id=story_id,
            ).returning(Post.id))).scalar_one()

    async def story_of(self, post_id: int) -> int | None:
        async with self.factory() as session:
            return (await session.execute(select(Post.story_id).where(Post.id == post_id))).scalar_one()


async def get_story(factory: async_sessionmaker[AsyncSession], story_id: int | None) -> Story:
    assert story_id is not None
    async with factory() as session:
        story = await session.get(Story, story_id)
        assert story is not None
        return story


async def count_stories(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(Story))).scalar_one()


async def test_first_post_starts_story(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source = await make_source()
    posts = Posts(session_factory)
    post_id = await posts.add(source, vec(), title="В Испании столкнулись поезда", text="Первый абзац.\nВторой.")

    result = await ClusteringService(session_factory).process_batch()

    assert (result.new_stories, result.attached) == (1, 0)
    story = await get_story(session_factory, await posts.story_of(post_id))
    assert story.title == "В Испании столкнулись поезда"
    assert story.lead == "Первый абзац."
    assert (story.posts_count, story.sources_count) == (1, 1)
    assert story.first_post_id == post_id
    assert story.started_at == T0


async def test_cross_source_copy_is_attached(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    tass, rbc = await make_source(), await make_source()
    posts = Posts(session_factory)
    first = await posts.add(tass, vec(), T0, title="Столкнулись поезда")
    second = await posts.add(rbc, vec(0.95), T0 + timedelta(hours=2), title="40 погибших при столкновении поездов")

    result = await ClusteringService(session_factory).process_batch()

    assert (result.new_stories, result.attached) == (1, 1)
    story_id = await posts.story_of(first)
    assert await posts.story_of(second) == story_id
    story = await get_story(session_factory, story_id)
    assert (story.posts_count, story.sources_count) == (2, 2)
    assert story.title == "40 погибших при столкновении поездов"  # из самого свежего поста
    assert (story.first_post_id, story.first_source_id) == (first, tass)  # первым был ТАСС
    assert story.last_post_at == T0 + timedelta(hours=2)


async def test_uncertain_similarity_starts_new_story(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    a, b = await make_source(), await make_source()
    posts = Posts(session_factory)
    await posts.add(a, vec())
    await posts.add(b, vec(0.88), T0 + timedelta(minutes=30))  # спорно: не склеиваем

    await ClusteringService(session_factory).process_batch()

    assert await count_stories(session_factory) == 2


async def test_same_story_on_different_days_is_not_attached(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    a, b = await make_source(), await make_source()
    posts = Posts(session_factory)
    await posts.add(a, vec(), T0, title="В аэропорту Внуково ввели ограничения")
    await posts.add(b, vec(0.97), T0 + timedelta(hours=8), title="В аэропорту Внуково ввели ограничения")

    await ClusteringService(session_factory).process_batch()

    assert await count_stories(session_factory) == 2


async def test_same_source_needs_higher_similarity(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source = await make_source()
    posts = Posts(session_factory)
    base = await posts.add(source, vec())
    similar = await posts.add(source, vec(0.92, axis=2), T0 + timedelta(minutes=10))  # хватило бы для разных изданий
    update = await posts.add(source, vec(0.96, axis=3), T0 + timedelta(minutes=20))

    await ClusteringService(session_factory).process_batch()

    assert await posts.story_of(similar) != await posts.story_of(base)
    assert await posts.story_of(update) == await posts.story_of(base)


async def test_processing_order_does_not_matter(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    early_source, late_source = await make_source(), await make_source()
    posts = Posts(session_factory)
    # Поздний пост попал в базу раньше раннего
    late = await posts.add(late_source, vec(), T0 + timedelta(hours=1), title="Поздний")
    early = await posts.add(early_source, vec(0.95), T0, title="Ранний")

    await ClusteringService(session_factory).process_batch()

    story = await get_story(session_factory, await posts.story_of(late))
    assert await posts.story_of(early) == story.id
    assert (story.first_post_id, story.first_source_id) == (early, early_source)
    assert story.title == "Поздний"


async def test_edited_post_stays_in_its_story(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    a, b = await make_source(), await make_source()
    posts = Posts(session_factory)
    first = await posts.add(a, vec(), title="Столкнулись поезда")
    service = ClusteringService(session_factory)
    await service.process_batch()
    story_id = await posts.story_of(first)

    # Пост отредактировали: он снова прошёл пайплайн, но привязка к сюжету сохранилась.
    # Новый вектор далёк от прежнего — пост всё равно не переезжает в другой сюжет.
    edited = await posts.add(b, vec(0.5), T0 + timedelta(hours=1), title="Уточнение: 68 погибших", story_id=story_id)
    await service.process_batch()

    assert await posts.story_of(edited) == story_id
    story = await get_story(session_factory, story_id)
    assert (story.posts_count, story.title) == (2, "Уточнение: 68 погибших")


async def test_similar_stories(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource, session: AsyncSession
) -> None:
    a, b, c = await make_source(), await make_source(), await make_source()
    posts = Posts(session_factory)
    crash = await posts.add(a, vec(), title="Столкнулись поезда")
    reaction = await posts.add(b, vec(0.88), T0 + timedelta(hours=1), title="Премьер потрясён катастрофой")
    await posts.add(c, vec(0.30), T0, title="Вышел FastAPI 1.0")
    await ClusteringService(session_factory).process_batch()

    crash_story = await posts.story_of(crash)
    assert crash_story is not None
    similar = await StoryRepository(session).similar(crash_story, min_similarity=0.86, window=timedelta(hours=48))

    assert [s.story_id for s in similar] == [await posts.story_of(reaction)]
    assert math.isclose(similar[0].similarity, 0.88, abs_tol=1e-3)


async def test_broken_post_does_not_break_batch(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    a, b = await make_source(), await make_source()
    posts = Posts(session_factory)
    good = await posts.add(a, vec())
    async with session_factory() as session, session.begin():  # пост без вектора: склейка упадёт
        broken = (await session.execute(insert(Post).values(
            source_id=b, external_id="broken", url="https://news.example/broken",
            url_normalized="https://news.example/broken", title="Битый", content_hash="x",
            status=PostStatus.EMBEDDED,
        ).returning(Post.id))).scalar_one()
    after = await posts.add(b, vec(0.95), T0 + timedelta(minutes=5))

    result = await ClusteringService(session_factory).process_batch()

    assert (result.errors, result.new_stories, result.attached) == (1, 1, 1)
    assert await posts.story_of(after) == await posts.story_of(good)
    async with session_factory() as session:
        row = (await session.execute(
            select(Post.status, Post.attempt, Post.last_error, Post.story_id).where(Post.id == broken)
        )).one()
    assert row == (PostStatus.EMBEDDED, 1, "ValueError: у поста нет вектора", None)


def angle(degrees: float) -> list[float]:
    """Единичный вектор в плоскости двух первых осей: сходство двух таких векторов — косинус угла между ними."""
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return v


async def test_story_does_not_drift_along_a_chain(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    a, b, c = await make_source(), await make_source(), await make_source()
    posts = Posts(session_factory)
    moscow = await posts.add(a, angle(0), T0, title="Атака на Москву")
    more = await posts.add(b, angle(18), T0 + timedelta(hours=1), title="Над Москвой сбили ещё 20")
    # Похож на предыдущий пост (0,95), но далёк от сюжета в целом: цепочка уходит в сторону
    kursk = await posts.add(c, angle(36), T0 + timedelta(hours=2), title="В Курской области сбили 150 БПЛА")

    await ClusteringService(session_factory).process_batch()

    assert await posts.story_of(more) == await posts.story_of(moscow)
    assert await posts.story_of(kursk) != await posts.story_of(moscow)


async def test_headline_is_typical_recent_post_not_a_side_one(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    sources = [await make_source() for _ in range(4)]
    posts = Posts(session_factory)
    first = await posts.add(sources[0], angle(0), T0, title="Атака на Москву")
    await posts.add(sources[1], angle(10), T0 + timedelta(hours=1), title="Над Москвой сбили 100 дронов")
    await posts.add(sources[2], angle(20), T0 + timedelta(hours=2), title="Над Москвой сбили 231 дрон")
    # Самый свежий, но боковой: приклеен (близок к соседу и центру), а для заголовка не годится
    await posts.add(sources[3], angle(28), T0 + timedelta(hours=2, minutes=30), title="Под Смоленском сбили 12 дронов")

    await ClusteringService(session_factory).process_batch()

    story = await get_story(session_factory, await posts.story_of(first))
    assert story.posts_count == 4
    assert story.title == "Над Москвой сбили 231 дрон"


async def test_reset_and_recluster(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    a, b = await make_source(), await make_source()
    posts = Posts(session_factory)
    first = await posts.add(a, vec())
    second = await posts.add(b, vec(0.95), T0 + timedelta(minutes=30))
    await posts.add(b, vec(0.3), T0, title="Другая новость")
    service = ClusteringService(session_factory)
    await service.process_batch()
    assert await count_stories(session_factory) == 2

    assert await service.reset() == 3
    assert await count_stories(session_factory) == 0
    assert await posts.story_of(first) is None

    result = await service.process_batch()

    assert (result.new_stories, result.attached) == (2, 1)
    assert await posts.story_of(first) == await posts.story_of(second)


def url_of(post_id_n: int) -> str:
    return f"https://news.example/{post_id_n}"


async def test_post_linking_to_other_outlet_joins_its_story(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """Канал пишет своими словами и ссылается на статью: сходство ниже порога, но это то же событие."""
    tass, channel = await make_source(name="ТАСС"), await make_source(name="Канал")
    posts = Posts(session_factory)
    article = await posts.add(tass, vec(), at=T0)
    await ClusteringService(session_factory).process_batch()

    retelling = await posts.add(channel, vec(0.85), at=T0 + timedelta(hours=2), links=[url_of(1)])
    result = await ClusteringService(session_factory).process_batch()

    assert (result.attached, result.by_link) == (1, 1)
    assert await posts.story_of(retelling) == await posts.story_of(article)


async def test_article_joins_story_of_post_that_linked_it_earlier(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """Пост канала пришёл раньше статьи, на которую ссылается: статья идёт в его сюжет."""
    tass, channel = await make_source(name="ТАСС"), await make_source(name="Канал")
    posts = Posts(session_factory)
    retelling = await posts.add(channel, vec(0.85), at=T0, links=[url_of(2)])
    await ClusteringService(session_factory).process_batch()

    article = await posts.add(tass, vec(), at=T0 + timedelta(minutes=10))
    await ClusteringService(session_factory).process_batch()

    assert await posts.story_of(article) == await posts.story_of(retelling)


async def test_links_within_one_outlet_and_old_links_do_not_join(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """«Читайте также» на своё издание и ссылка на позавчерашнюю статью — другие события."""
    outlet, other = await make_source(name="iXBT"), await make_source(name="РГ")
    posts = Posts(session_factory)
    await posts.add(outlet, vec(), at=T0)
    await posts.add(other, vec(), at=T0, title="Старая статья")
    await ClusteringService(session_factory).process_batch()

    see_also = await posts.add(outlet, vec(0.85), at=T0 + timedelta(hours=1), links=[url_of(1)])
    late = await posts.add(outlet, vec(0.85, axis=2), at=T0 + timedelta(hours=30), links=[url_of(2)])
    result = await ClusteringService(session_factory).process_batch()

    assert (result.new_stories, result.by_link) == (2, 0)
    assert len({await posts.story_of(see_also), await posts.story_of(late)}) == 2
