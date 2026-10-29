from dataclasses import dataclass
from datetime import datetime, timedelta

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Story
from sito.models.post import EMBEDDING_DIM, EVENT_AT_SQL

LEAD_MAX_CHARS = 300  # ТЗ: из чужих материалов показываем не больше первого абзаца, около 300 знаков


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """Пост из уже существующего сюжета, похожий на новый пост."""

    post_id: int
    story_id: int
    source_id: int
    similarity: float
    event_at: datetime


@dataclass(frozen=True, slots=True)
class SimilarStory:
    story_id: int
    title: str
    similarity: float


_vector = bindparam("embedding", type_=Vector(EMBEDDING_DIM))

_CANDIDATES_SQL = text(f"""
    SELECT post.id AS post_id, post.story_id, post.source_id,
           1 - (post.embedding <=> :embedding) AS similarity,
           {EVENT_AT_SQL} AS event_at
    FROM post
    WHERE post.story_id IS NOT NULL
      AND post.embedding IS NOT NULL
      AND post.id <> :post_id
      AND {EVENT_AT_SQL} BETWEEN :since AND :until
    ORDER BY post.embedding <=> :embedding
    LIMIT :limit
""").bindparams(_vector)

# Сводка сюжета по его постам: счётчики, первый пост, самый свежий пост, средний вектор
_REFRESH_SQL = text(f"""
    WITH members AS (
        SELECT post.id, post.source_id, post.title, post.text, post.embedding, {EVENT_AT_SQL} AS event_at
        FROM post WHERE post.story_id = :story_id
    ),
    totals AS (
        SELECT count(*) AS posts, count(DISTINCT source_id) AS sources,
               min(event_at) AS started, max(event_at) AS last, avg(embedding) AS centroid
        FROM members
    ),
    first_post AS (SELECT id, source_id FROM members ORDER BY event_at, id LIMIT 1),
    latest_post AS (SELECT title, text FROM members ORDER BY event_at DESC, id DESC LIMIT 1)
    UPDATE story SET
        posts_count = totals.posts,
        sources_count = totals.sources,
        started_at = totals.started,
        last_post_at = totals.last,
        centroid = totals.centroid,
        first_post_id = first_post.id,
        first_source_id = first_post.source_id,
        title = latest_post.title,
        lead = nullif(left(split_part(coalesce(latest_post.text, ''), E'\\n', 1), :lead_max), ''),
        updated_at = now()
    FROM totals, first_post, latest_post
    WHERE story.id = :story_id
""")

_SIMILAR_SQL = text("""
    SELECT other.id AS story_id, other.title, 1 - (other.centroid <=> this.centroid) AS similarity
    FROM story AS this
    JOIN story AS other
      ON other.id <> this.id
     AND other.last_post_at >= this.started_at - CAST(:window AS interval)
     AND other.started_at <= this.last_post_at + CAST(:window AS interval)
    WHERE this.id = :story_id
      AND 1 - (other.centroid <=> this.centroid) >= :min_similarity
    ORDER BY other.centroid <=> this.centroid
    LIMIT :limit
""")


class StoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, story_id: int) -> Story | None:
        return await self.session.get(Story, story_id)

    async def create(self, title: str, event_at: datetime) -> Story:
        """Пустой сюжет. Остальные поля заполнит refresh после привязки постов."""
        story = Story(title=title, started_at=event_at, last_post_at=event_at)
        self.session.add(story)
        await self.session.flush()
        return story

    async def match_candidates(
        self, post_id: int, embedding: list[float], event_at: datetime, max_gap: timedelta, limit: int = 20
    ) -> list[MatchCandidate]:
        """Самые похожие посты из сюжетов, опубликованные не дальше max_gap от нового поста."""
        result = await self.session.execute(_CANDIDATES_SQL, {
            "embedding": embedding,
            "post_id": post_id,
            "since": event_at - max_gap,
            "until": event_at + max_gap,
            "limit": limit,
        })
        return [MatchCandidate(**row._mapping) for row in result]

    async def refresh(self, story_id: int) -> None:
        """Пересчитать сводку сюжета по его постам. Порядок обработки постов не важен."""
        await self.session.execute(_REFRESH_SQL, {"story_id": story_id, "lead_max": LEAD_MAX_CHARS})

    async def similar(
        self, story_id: int, min_similarity: float, window: timedelta, limit: int = 5
    ) -> list[SimilarStory]:
        """Похожие сюжеты для страницы сюжета: близкие по смыслу и по времени."""
        result = await self.session.execute(_SIMILAR_SQL, {
            "story_id": story_id, "min_similarity": min_similarity, "window": window, "limit": limit,
        })
        return [SimilarStory(**row._mapping) for row in result]
