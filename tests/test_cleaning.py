import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus
from sito.services import cleaning
from sito.services.cleaning import MAX_ATTEMPTS, CleaningService
from tests.conftest import MakeSource


async def add_post(
    factory: async_sessionmaker[AsyncSession], source_id: int, n: int, title: str, raw_text: str | None
) -> int:
    async with factory() as session, session.begin():
        return (await session.execute(insert(Post).values(
            source_id=source_id,
            external_id=f"id-{n}",
            url=f"https://news.example/{n}",
            url_normalized=f"https://news.example/{n}",
            title=title,
            raw_text=raw_text,
            content_hash=f"h{n}",
        ).returning(Post.id))).scalar_one()


async def get_post(factory: async_sessionmaker[AsyncSession], post_id: int) -> Post:
    async with factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        return post


async def test_cleans_new_posts(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> None:
    source_id = await make_source()
    post_id = await add_post(
        session_factory, source_id, 1, "Уголь\xa0в Кузбассе",
        '<p>Добыча <a href="https://other.example/a?utm_source=x">выросла</a>.</p><p>Читать далее</p>',
    )

    result = await CleaningService(session_factory).process_batch()

    assert (result.cleaned, result.skipped, result.errors) == (1, 0, 0)
    post = await get_post(session_factory, post_id)
    assert post.status is PostStatus.CLEANED
    assert post.title == "Уголь в Кузбассе"
    assert post.text == "Добыча выросла."
    assert post.links == ["https://other.example/a"]
    assert post.raw_text is not None  # оригинал сохраняется


async def test_post_without_title_gets_one_from_text(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """У постов Telegram заголовка нет: им становится первая фраза текста."""
    source_id = await make_source()
    post_id = await add_post(
        session_factory, source_id, 1, "", "<p>На шахте «Листвяжная» возобновили добычу. Об этом сообщили в компании.</p>"
    )

    await CleaningService(session_factory).process_batch()

    post = await get_post(session_factory, post_id)
    assert post.status is PostStatus.CLEANED
    assert post.title == "На шахте «Листвяжная» возобновили добычу"


async def test_title_only_is_not_junk(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    post_id = await add_post(session_factory, source_id, 1, "МИД допустил разрыв отношений", None)

    await CleaningService(session_factory).process_batch()

    post = await get_post(session_factory, post_id)
    assert post.status is PostStatus.CLEANED
    assert post.text is None


async def test_ads_and_empty_posts_are_skipped(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    ad = await add_post(session_factory, source_id, 1, "Скидки", "<p>Купите!</p><p>Реклама. erid: 2Vtzqx</p>")
    empty = await add_post(session_factory, source_id, 2, " ", "<img src='https://x/y.jpg'>")

    result = await CleaningService(session_factory).process_batch()

    assert result.skipped == 2
    assert (await get_post(session_factory, ad)).status is PostStatus.SKIPPED
    assert (await get_post(session_factory, empty)).status is PostStatus.SKIPPED


async def test_only_new_posts_are_taken(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    await add_post(session_factory, source_id, 1, "Первый", "<p>Текст</p>")
    service = CleaningService(session_factory)
    await service.process_batch()

    result = await service.process_batch()

    assert result.total == 0


async def test_batch_size(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> None:
    source_id = await make_source()
    for n in range(5):
        await add_post(session_factory, source_id, n, f"Пост {n}", "<p>Текст</p>")
    service = CleaningService(session_factory, batch_size=3)

    assert (await service.process_batch()).cleaned == 3
    assert (await service.process_batch()).cleaned == 2


async def test_errors_retry_then_fail(
    session_factory: async_sessionmaker[AsyncSession],
    make_source: MakeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(raw: str | None, own_url: str | None = None) -> None:
        raise RuntimeError("сломалось")

    monkeypatch.setattr(cleaning, "clean_html", broken)
    source_id = await make_source()
    post_id = await add_post(session_factory, source_id, 1, "Пост", "<p>Текст</p>")
    service = CleaningService(session_factory)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await service.process_batch()
        assert result.errors == 1
        post = await get_post(session_factory, post_id)
        assert post.attempt == attempt
        assert post.last_error == "RuntimeError: сломалось"

    assert post.status is PostStatus.FAILED
    assert (await service.process_batch()).total == 0  # больше не берётся


async def test_edited_post_is_cleaned_again(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    from sito.repositories.posts import NewPost, PostRepository

    source_id = await make_source()
    post_id = await add_post(session_factory, source_id, 1, "Пост", '<p><a href="https://a.example/1">a</a></p>')
    service = CleaningService(session_factory)
    await service.process_batch()

    # Источник отредактировал пост: upsert сбрасывает его в new вместе с текстом и ссылками
    async with session_factory() as session, session.begin():
        await PostRepository(session).upsert_many([NewPost(
            source_id=source_id, external_id="id-1", url="https://news.example/1",
            url_normalized="https://news.example/1", title="Пост (исправлено)",
            raw_text="<p>Новый текст</p>", content_hash="h1-v2", published_at=None,
        )])
    async with session_factory() as session:
        reset = (await session.execute(select(Post.status, Post.links).where(Post.id == post_id))).one()
    assert reset == (PostStatus.NEW, [])

    await service.process_batch()

    post = await get_post(session_factory, post_id)
    assert (post.status, post.text, post.links) == (PostStatus.CLEANED, "Новый текст", [])


async def test_chatter_without_title_is_skipped(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """«Бггггг» в Telegram — болтовня. Заголовок поста — его же первая фраза, и повтор не делает
    слово «именем» (раньше «Бггггг\\nБггггг» проходило в сюжеты и в поиск)."""
    source_id = await make_source()
    junk = [await add_post(session_factory, source_id, n, "", raw)
            for n, raw in enumerate(["Бггггг", 'Бггггг<a href="https://t.me/x/1"></a>', "ОК от униан))"])]
    # Новость из одной фразы, где имя — первое слово, не болтовня
    news = [await add_post(session_factory, source_id, 10 + n, "", raw) for n, raw in enumerate([
        "Новости дня\nПутин провёл совещание", "Трамп допустил перестановки в администрации",
        "Гурцкая призналась, что не может пережить смерть супруга",
        "<b>Лавров: ОБСЕ в плачевном состоянии</b>",  # весь пост — заголовок, тела нет
    ])]

    await CleaningService(session_factory).process_batch()

    assert [(await get_post(session_factory, i)).status for i in junk] == [PostStatus.SKIPPED] * 3
    assert [(await get_post(session_factory, i)).status for i in news] == [PostStatus.CLEANED] * 4


async def test_channel_byline_is_not_a_title(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    """Агрегатор ставит первой строкой имя канала-первоисточника у каждого репоста: это подпись, а не заголовок."""
    aggregator, channel = await make_source(), await make_source()
    reposts = [await add_post(session_factory, aggregator, n, "", raw) for n, raw in enumerate([
        "<strong>Открытый Город</strong>\nfrom <strong>INSIDER ELITES</strong>\nФНС вскрыла схемы угольной компании.",
        "<strong>Открытый Город</strong>\nВ Юрге посчитали стоимость отлова собак. Подробности позже.",
        "<strong>Открытый Город</strong>\n\nНа руднике установили остановку для работников.",
    ])]
    # Разные жирные заголовки у одного канала — настоящие заголовки
    own = [await add_post(session_factory, channel, 10 + n, "", f"<b>{title}</b>\nТекст новости.")
           for n, title in enumerate(["Взрыв в Кемерове", "Пожар на шахте", "Новый мэр Юрги"])]

    await CleaningService(session_factory).process_batch()

    assert [(await get_post(session_factory, i)).title for i in reposts] == [
        "ФНС вскрыла схемы угольной компании", "В Юрге посчитали стоимость отлова собак",
        "На руднике установили остановку для работников",
    ]
    assert "Открытый Город" not in ((await get_post(session_factory, reposts[0])).text or "")
    assert [(await get_post(session_factory, i)).title for i in own] == [
        "Взрыв в Кемерове", "Пожар на шахте", "Новый мэр Юрги",
    ]
