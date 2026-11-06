"""EmbeddingService с фальшивой моделью: настоящая весит гигабайт и грузится долго."""

import math
from collections.abc import Sequence

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus
from sito.models.post import EMBEDDING_DIM
from sito.repositories.posts import NewPost, PostRepository
from sito.services.embedding import Embedder, EmbeddingService, embedding_text
from sito.services.pipeline import MAX_ATTEMPTS
from tests.conftest import MakeSource


class FakeEmbedder(Embedder):
    """Вектор из длины текста: детерминированный и нормализованный."""

    def __init__(self, fail: bool = False, bad: str | None = None) -> None:
        self.name = "fake-model"
        self.dim = EMBEDDING_DIM
        self.fail = fail
        self.bad = bad  # текст с этим кусочком модель не переваривает
        self.received: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail or (self.bad and any(self.bad in t for t in texts)):
            raise RuntimeError("модель упала")
        self.received.extend(texts)
        return [self._vector(len(t)) for t in texts]

    @staticmethod
    def _vector(seed: int) -> list[float]:
        raw = [float((seed + i) % 7 + 1) for i in range(EMBEDDING_DIM)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]


async def add_post(
    factory: async_sessionmaker[AsyncSession], source_id: int, n: int,
    status: PostStatus = PostStatus.CLEANED, text: str | None = "Текст новости", attempt: int = 0,
) -> int:
    async with factory() as session, session.begin():
        return (await session.execute(insert(Post).values(
            source_id=source_id, external_id=f"id-{n}", url=f"https://news.example/{n}",
            url_normalized=f"https://news.example/{n}", title=f"Заголовок {n}", text=text,
            content_hash=f"h{n}", status=status, attempt=attempt,
        ).returning(Post.id))).scalar_one()


async def get_post(factory: async_sessionmaker[AsyncSession], post_id: int) -> Post:
    async with factory() as session:
        post = await session.get(Post, post_id)
        assert post is not None
        return post


def test_embedding_text_joins_title_and_start() -> None:
    assert embedding_text("Заголовок", "Первый абзац.\nВторой.") == "Заголовок\nПервый абзац. Второй."
    assert embedding_text("Только заголовок", None) == "Только заголовок"


def test_embedding_text_cuts_at_word_boundary() -> None:
    result = embedding_text("З", "слово " * 300, max_chars=20)

    assert result == "З\nслово слово слово"


async def test_embeds_cleaned_posts(session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource) -> None:
    source_id = await make_source()
    post_id = await add_post(session_factory, source_id, 1)
    embedder = FakeEmbedder()

    result = await EmbeddingService(session_factory, embedder).process_batch()

    assert (result.embedded, result.errors) == (1, 0)
    assert embedder.received == ["Заголовок 1\nТекст новости"]
    post = await get_post(session_factory, post_id)
    assert post.status is PostStatus.EMBEDDED
    assert post.embedding_model == "fake-model"
    assert post.embedding is not None
    # Вектор пережил запись в pgvector и чтение обратно
    assert len(post.embedding) == EMBEDDING_DIM
    assert math.isclose(sum(x * x for x in post.embedding), 1.0, rel_tol=1e-5)


async def test_only_cleaned_posts_are_taken(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    for n, status in enumerate([PostStatus.NEW, PostStatus.SKIPPED, PostStatus.FAILED, PostStatus.EMBEDDED]):
        await add_post(session_factory, source_id, n, status=status)

    result = await EmbeddingService(session_factory, FakeEmbedder()).process_batch()

    assert result.total == 0


async def test_success_resets_attempts_from_previous_step(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    post_id = await add_post(session_factory, source_id, 1, attempt=2)  # на очистке было 2 неудачи

    await EmbeddingService(session_factory, FakeEmbedder()).process_batch()

    assert (await get_post(session_factory, post_id)).attempt == 0


async def test_model_error_counts_for_whole_batch(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    ids = [await add_post(session_factory, source_id, n) for n in range(2)]
    service = EmbeddingService(session_factory, FakeEmbedder(fail=True))

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await service.process_batch()
        assert result.errors == 2
        assert {(await get_post(session_factory, i)).attempt for i in ids} == {attempt}

    for post_id in ids:
        post = await get_post(session_factory, post_id)
        assert post.status is PostStatus.FAILED
        assert post.last_error == "RuntimeError: модель упала"
        assert post.embedding is None


async def test_one_bad_text_does_not_fail_the_batch(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    good = await add_post(session_factory, source_id, 1)
    bad = await add_post(session_factory, source_id, 2, text="битый текст")

    result = await EmbeddingService(session_factory, FakeEmbedder(bad="битый")).process_batch()

    assert (result.embedded, result.errors) == (1, 1)
    assert (await get_post(session_factory, good)).status is PostStatus.EMBEDDED
    assert (await get_post(session_factory, bad)).attempt == 1


async def test_edited_post_drops_embedding(
    session_factory: async_sessionmaker[AsyncSession], make_source: MakeSource
) -> None:
    source_id = await make_source()
    post_id = await add_post(session_factory, source_id, 1)
    await EmbeddingService(session_factory, FakeEmbedder()).process_batch()

    async with session_factory() as session, session.begin():
        await PostRepository(session).upsert_many([NewPost(
            source_id=source_id, external_id="id-1", url="https://news.example/1",
            url_normalized="https://news.example/1", title="Исправлено", raw_text=None,
            content_hash="h1-v2", published_at=None,
        )])

    async with session_factory() as session:
        row = (await session.execute(
            select(Post.status, Post.embedding, Post.embedding_model).where(Post.id == post_id)
        )).one()
    assert row == (PostStatus.NEW, None, None)
