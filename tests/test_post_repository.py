from dataclasses import replace

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Post, PostStatus
from sito.repositories.posts import NewPost, PostRepository
from tests.conftest import MakeSource


def new_post(source_id: int, n: int, content: str = "v1") -> NewPost:
    return NewPost(
        source_id=source_id,
        external_id=f"id-{n}",
        url=f"https://news.example/{n}",
        url_normalized=f"https://news.example/{n}",
        title=f"Пост {n}",
        raw_text="<p>текст</p>",
        content_hash=content,
        published_at=None,
    )


async def test_inserts_new_posts(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source()
    repo = PostRepository(session)

    result = await repo.upsert_many([new_post(source_id, 1), new_post(source_id, 2)])

    assert (result.inserted, result.updated, result.unchanged) == (2, 0, 0)


async def test_repeat_is_unchanged(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source()
    repo = PostRepository(session)
    posts = [new_post(source_id, 1), new_post(source_id, 2)]
    await repo.upsert_many(posts)

    result = await repo.upsert_many(posts)

    assert (result.inserted, result.updated, result.unchanged) == (0, 0, 2)


async def test_edited_post_is_updated_and_reprocessed(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source()
    repo = PostRepository(session)
    await repo.upsert_many([new_post(source_id, 1)])
    # Пост успел пройти часть пайплайна
    await session.execute(update(Post).values(status=PostStatus.CLEANED, text="очищено", attempt=2))

    result = await repo.upsert_many([replace(new_post(source_id, 1, content="v2"), title="Исправлено")])

    assert (result.inserted, result.updated) == (0, 1)
    post = (await session.execute(select(Post))).scalar_one()
    await session.refresh(post)
    assert post.title == "Исправлено"
    assert post.status is PostStatus.NEW  # заново через весь пайплайн
    assert post.text is None
    assert post.attempt == 0


async def test_duplicates_inside_batch(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source()
    repo = PostRepository(session)

    # Без дедупликации Postgres упал бы: «cannot affect row a second time»
    result = await repo.upsert_many([new_post(source_id, 1), new_post(source_id, 1, content="v2")])

    assert result.inserted == 1
    stored = (await session.execute(select(Post.content_hash))).scalar_one()
    assert stored == "v2"  # побеждает последний


async def test_url_owners(session: AsyncSession, make_source: MakeSource) -> None:
    source_id = await make_source()
    repo = PostRepository(session)
    await repo.upsert_many([new_post(source_id, 1)])

    owners = await repo.url_owners(["https://news.example/1", "https://news.example/unknown"])

    assert owners == {"https://news.example/1": (source_id, "id-1")}
    assert await repo.url_owners([]) == {}
