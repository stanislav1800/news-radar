from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import Boolean, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Post, PostStatus
from sito.models.post import EVENT_AT_SQL

# Своё пространство ключей advisory-блокировок для адресов статей (двухключевая форма)
URL_LOCK_SPACE = 7101


@dataclass(frozen=True, slots=True)
class NewPost:
    source_id: int
    external_id: str
    url: str
    url_normalized: str
    title: str
    raw_text: str | None
    content_hash: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class UpsertResult:
    inserted: int
    updated: int
    unchanged: int


class PostRepository:
    CHUNK_SIZE = 1000

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def take_pending(self, status: PostStatus, limit: int, by_event_time: bool = False) -> list[Post]:
        """Пачка постов в статусе status, заблокированная до конца транзакции.

        SKIP LOCKED: несколько обработчиков параллельно берут разные посты, не дожидаясь друг друга.
        by_event_time: по времени новости, а не по порядку записи в базу (для склейки).
        """
        query = select(Post).where(Post.status == status).limit(limit).with_for_update(skip_locked=True)
        if by_event_time:
            query = query.order_by(literal_column(EVENT_AT_SQL), Post.id)
        else:
            query = query.order_by(Post.id)
        result = await self.session.scalars(query)
        return list(result)

    async def lock_urls(self, urls: Collection[str]) -> None:
        """Блокировка адресов до конца транзакции: две ленты одного издания с одной статьёй сохраняются
        по очереди, и вторая увидит статью первой, а не упадёт на уникальности url_normalized.
        По порядку — чтобы встречные пачки не заблокировали друг друга."""
        if urls:
            await self.session.execute(
                text("SELECT count(pg_advisory_xact_lock(:space, hashtext(u))) FROM unnest(CAST(:urls AS text[])) u"),
                {"space": URL_LOCK_SPACE, "urls": sorted(urls)},
            )

    async def url_owners(self, urls: Collection[str]) -> dict[str, tuple[int, str]]:
        """Какие из адресов уже есть в базе и чьи они: url → (source_id, external_id)."""
        if not urls:
            return {}
        result = await self.session.execute(
            select(Post.url_normalized, Post.source_id, Post.external_id)
            .where(Post.url_normalized.in_(urls))
        )
        return {url: (source_id, external_id) for url, source_id, external_id in result}

    async def upsert_many(self, posts: Sequence[NewPost]) -> UpsertResult:
        unique = list({(p.source_id, p.external_id): p for p in posts}.values())

        inserted = updated = 0
        for start in range(0, len(unique), self.CHUNK_SIZE):
            chunk = unique[start:start + self.CHUNK_SIZE]

            stmt = insert(Post).values([asdict(p) for p in chunk])
            new = stmt.excluded  # строка, которую пытались вставить
            upsert = stmt.on_conflict_do_update(
                constraint="uq_post_source_id_external_id",
                set_={
                    "url": new.url,
                    "url_normalized": new.url_normalized,
                    "title": new.title,
                    "raw_text": new.raw_text,
                    "content_hash": new.content_hash,
                    "published_at": new.published_at,
                    "text": None,
                    "links": [],
                    "embedding": None,
                    "embedding_model": None,
                    "status": PostStatus.NEW,
                    "attempt": 0,
                    "last_error": None,
                    "updated_at": func.now(),
                },
                where=Post.content_hash != new.content_hash,
            ).returning(literal_column("xmax = 0", Boolean))  # True — вставлена, False — обновлена

            flags = (await self.session.execute(upsert)).scalars().all()
            inserted += sum(flags)
            updated += len(flags) - sum(flags)

        return UpsertResult(inserted, updated, len(unique) - inserted - updated)