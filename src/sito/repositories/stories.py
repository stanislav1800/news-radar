from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ColumnElement,
    DateTime,
    Select,
    bindparam,
    case,
    false,
    func,
    literal_column,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sito.models import Post, Source, Story, StoryRead, TopicMatch
from sito.models.post import (
    EMBEDDING_DIM,
    EVENT_AT_SQL,
    POST_FTS_SQL,
    STORY_HAS_TEXT_SQL,
)

LEAD_MAX_CHARS = 300  # ТЗ: из чужих материалов показываем не больше первого абзаца, около 300 знаков
HEADLINE_WINDOW = timedelta(hours=3)  # заголовок выбирается среди постов последних часов сюжета
HEADLINE_SLACK = 0.02                 # «типичный» пост: не дальше от центра, чем самый типичный, на столько


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """Пост из уже существующего сюжета, похожий на новый пост."""

    post_id: int
    story_id: int
    source_id: int
    similarity: float               # с этим постом
    story_similarity: float | None  # с центром его сюжета (None, если у сюжета ещё нет центра)
    event_at: datetime


@dataclass(frozen=True, slots=True)
class SimilarStory:
    story_id: int
    title: str
    similarity: float
    sources_count: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class FeedStory:
    """Карточка сюжета в ленте."""

    id: int
    title: str
    lead: str | None
    sources_count: int
    posts_count: int
    started_at: datetime
    last_post_at: datetime
    first_source_name: str | None
    read_state: str  # new — не открывал; updated — открывал, но с тех пор появились посты; read


@dataclass(frozen=True, slots=True)
class TimelinePost:
    """Пост в хронологии сюжета. Полный текст чужих материалов не показываем (ТЗ)."""

    id: int
    title: str
    url: str
    source_name: str
    event_at: datetime


_vector = bindparam("embedding", type_=Vector(EMBEDDING_DIM))

_CANDIDATES_SQL = text(f"""
    SELECT post.id AS post_id, post.story_id, post.source_id,
           1 - (post.embedding <=> :embedding) AS similarity,
           1 - (story.centroid <=> :embedding) AS story_similarity,
           {EVENT_AT_SQL} AS event_at
    FROM post
    JOIN story ON story.id = post.story_id
    WHERE post.story_id IS NOT NULL
      AND post.embedding IS NOT NULL
      AND post.id <> :post_id
      AND {EVENT_AT_SQL} BETWEEN :since AND :until
    ORDER BY post.embedding <=> :embedding
    LIMIT :limit
""").bindparams(_vector)

# Сводка сюжета по его постам: счётчики, первый пост, средний вектор и пост для заголовка.
# Заголовок — самый свежий пост среди «типичных» за последние часы: свежий, но про суть сюжета.
# Просто самый свежий пост бывает боковым (реакция, соседний регион) и подменяет смысл сюжета.
# Посты из сюжетов, связанные с новым постом ссылкой: он ссылается на них или они на него.
# Только другие издания (своё издание ссылается на «читайте также») и только близко по времени.
_LINK_CANDIDATES_SQL = text(f"""
    SELECT post.id AS post_id, post.story_id, post.source_id,
           1 - (post.embedding <=> :embedding) AS similarity,
           NULL::float AS story_similarity,
           {EVENT_AT_SQL} AS event_at
    FROM post
    WHERE post.story_id IS NOT NULL
      AND post.embedding IS NOT NULL
      AND post.id <> :post_id
      AND post.source_id <> :source_id
      AND (post.url_normalized = ANY(CAST(:links AS text[])) OR post.links @> ARRAY[CAST(:url AS text)])
      AND {EVENT_AT_SQL} BETWEEN :since AND :until
    ORDER BY post.embedding <=> :embedding
""").bindparams(bindparam("embedding", type_=Vector(EMBEDDING_DIM)))

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
    recent AS (
        SELECT members.*, 1 - (members.embedding <=> totals.centroid) AS centrality
        FROM members, totals
        WHERE members.event_at >= totals.last - CAST(:headline_window AS interval)
    ),
    latest_post AS (
        SELECT title, text FROM recent
        WHERE centrality IS NULL OR centrality >= (SELECT max(centrality) FROM recent) - :headline_slack
        ORDER BY event_at DESC, id DESC LIMIT 1
    )
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
        -- Когда сюжет изменился у нас (приклеился пост): по нему считается «есть обновления».
        -- clock_timestamp, а не now(): now() — время начала транзакции, а не текущее.
        updated_at = clock_timestamp()
    FROM totals, first_post, latest_post
    WHERE story.id = :story_id
""")

_SIMILAR_SQL = text("""
    SELECT other.id AS story_id, other.title, 1 - (other.centroid <=> this.centroid) AS similarity,
           other.sources_count, other.started_at
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


# Поиск по словам: сюжеты, у которых хотя бы один пост содержит слова запроса.
# Выражение слов — POST_FTS_SQL, дословно как в индексе ix_post_fts.
_SEARCH_WORDS_SQL = text(f"""
    SELECT story.id FROM story
    WHERE story.id IN (
        SELECT post.story_id FROM post
        WHERE {POST_FTS_SQL} @@ websearch_to_tsquery('russian', :query)
    )
      AND (CAST(:since AS timestamptz) IS NULL OR story.last_post_at >= :since)
    ORDER BY story.started_at DESC, story.id DESC
    LIMIT :limit
""")

_SEARCH_MEANING_SQL = text(f"""
    SELECT story.id FROM story
    WHERE story.centroid IS NOT NULL AND {STORY_HAS_TEXT_SQL}
      AND (CAST(:since AS timestamptz) IS NULL OR story.last_post_at >= :since)
      AND story.id <> ALL(CAST(:exclude AS bigint[]))
      AND 1 - (story.centroid <=> :embedding) >= :min_similarity
    ORDER BY story.centroid <=> :embedding
    LIMIT :limit
""").bindparams(bindparam("embedding", type_=Vector(EMBEDDING_DIM)))


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

    async def link_candidates(
        self, post_id: int, source_id: int, url: str, links: Sequence[str],
        embedding: list[float], event_at: datetime, max_gap: timedelta,
    ) -> list[MatchCandidate]:
        """Посты из сюжетов другого издания, связанные с постом ссылкой в ту или другую сторону."""
        result = await self.session.execute(_LINK_CANDIDATES_SQL, {
            "embedding": embedding, "post_id": post_id, "source_id": source_id,
            "url": url, "links": list(links),
            "since": event_at - max_gap, "until": event_at + max_gap,
        })
        return [MatchCandidate(**row._mapping) for row in result]

    async def refresh(self, story_id: int) -> None:
        """Пересчитать сводку сюжета по его постам. Порядок обработки постов не важен."""
        await self.session.execute(_REFRESH_SQL, {
            "story_id": story_id,
            "lead_max": LEAD_MAX_CHARS,
            "headline_window": HEADLINE_WINDOW,
            "headline_slack": HEADLINE_SLACK,
        })

    async def similar(
        self, story_id: int, min_similarity: float, window: timedelta, limit: int = 5
    ) -> list[SimilarStory]:
        """Похожие сюжеты для страницы сюжета: близкие по смыслу и по времени."""
        result = await self.session.execute(_SIMILAR_SQL, {
            "story_id": story_id, "min_similarity": min_similarity, "window": window, "limit": limit,
        })
        return [SimilarStory(**row._mapping) for row in result]

    @staticmethod
    def _cards(user_id: int, read_all_before: datetime | None) -> tuple[Select[Any], ColumnElement[str]]:
        """Запрос карточек сюжетов с отметками прочитанного для пользователя и выражение read_state."""
        # Сравниваем с моментом, когда сюжет изменился у нас, а не со временем публикации:
        # пост, опубликованный до прочтения, но скачанный после, — тоже обновление
        read_at = StoryRead.read_at
        is_read = or_(
            read_at >= Story.updated_at,
            Story.updated_at <= read_all_before if read_all_before else false(),
        )
        read_state = case((is_read, "read"), (read_at.is_not(None), "updated"), else_="new")
        query = (
            select(
                Story.id, Story.title, Story.lead, Story.sources_count, Story.posts_count,
                Story.started_at, Story.last_post_at, Source.name.label("first_source_name"),
                read_state.label("read_state"),
            )
            .outerjoin(Source, Source.id == Story.first_source_id)
            .outerjoin(StoryRead, (StoryRead.story_id == Story.id) & (StoryRead.user_id == user_id))
        )
        return query, read_state

    async def feed(
        self,
        limit: int,
        *,
        user_id: int,
        read_all_before: datetime | None = None,
        min_sources: int = 1,
        before: tuple[datetime, int] | None = None,
        unread_only: bool = False,
        topic_ids: Sequence[int] | None = None,
    ) -> list[FeedStory]:
        """Сюжеты, новые сверху (по времени первого поста), с отметками прочитанного для пользователя.

        before — курсор для «Показать ещё». read_all_before — момент «Отметить всё прочитанным».
        topic_ids — персональная лента: только сюжеты, которые подошли к этим темам.
        """
        query, read_state = self._cards(user_id, read_all_before)
        query = (
            query.where(Story.sources_count >= min_sources)
            .order_by(Story.started_at.desc(), Story.id.desc())
            .limit(limit)
        )
        if before is not None:
            # Курсор по (время, id), а не OFFSET: новые сюжеты наверху не сдвигают уже показанные
            query = query.where(tuple_(Story.started_at, Story.id) < tuple_(*before))
        if unread_only:
            query = query.where(read_state != "read")
        if topic_ids is not None:
            matched = select(TopicMatch.story_id).where(
                TopicMatch.story_id == Story.id, TopicMatch.topic_id.in_(topic_ids)
            )
            query = query.where(matched.exists())
        return [FeedStory(**row._mapping) for row in await self.session.execute(query)]

    async def cards(
        self, story_ids: Sequence[int], *, user_id: int, read_all_before: datetime | None = None
    ) -> list[FeedStory]:
        """Карточки сюжетов в порядке story_ids: для поиска и подсказок."""
        if not story_ids:
            return []
        query, _ = self._cards(user_id, read_all_before)
        rows = {row.id: FeedStory(**row._mapping) for row in await self.session.execute(
            query.where(Story.id.in_(story_ids))
        )}
        return [rows[story_id] for story_id in story_ids if story_id in rows]

    async def search_words(self, query: str, since: datetime | None, limit: int) -> list[int]:
        """Сюжеты, в постах которых есть слова запроса. Свежие сверху.

        Запрос в синтаксисе websearch: слова через пробел — все, «or» — любое, -слово — без него,
        "фраза в кавычках" — подряд.
        """
        result = await self.session.execute(_SEARCH_WORDS_SQL, {"query": query, "since": since, "limit": limit})
        return list(result.scalars())

    async def search_meaning(
        self, embedding: list[float], since: datetime | None, limit: int, min_similarity: float,
        exclude: Sequence[int] = (),
    ) -> list[int]:
        """Сюжеты, близкие к запросу по смыслу. Самые похожие сверху. Сюжеты без текста не участвуют."""
        result = await self.session.execute(_SEARCH_MEANING_SQL, {
            "embedding": embedding, "since": since, "limit": limit,
            "min_similarity": min_similarity, "exclude": list(exclude),
        })
        return list(result.scalars())

    async def mark_read(self, user_id: int, story_id: int) -> None:
        stmt = insert(StoryRead).values(user_id=user_id, story_id=story_id, read_at=func.clock_timestamp())
        await self.session.execute(
            stmt.on_conflict_do_update(index_elements=["user_id", "story_id"], set_={"read_at": stmt.excluded.read_at})
        )

    async def timeline(self, story_id: int) -> list[TimelinePost]:
        """Посты сюжета по времени: как развивалось событие."""
        event_at = literal_column(EVENT_AT_SQL, DateTime(timezone=True))
        query = (
            select(Post.id, Post.title, Post.url, Source.name.label("source_name"), event_at.label("event_at"))
            .join(Source, Source.id == Post.source_id)
            .where(Post.story_id == story_id)
            .order_by(event_at, Post.id)
        )
        return [TimelinePost(**row._mapping) for row in await self.session.execute(query)]

    async def reset_all(self) -> int:
        """Снять со всех постов привязку к сюжетам и удалить сюжеты. Возвращает число постов к пересклейке."""
        result = await self.session.execute(text("""
            UPDATE post SET story_id = NULL, status = 'embedded', attempt = 0, last_error = NULL
            WHERE status = 'clustered' OR story_id IS NOT NULL
        """))
        await self.session.execute(text("DELETE FROM story"))
        return result.rowcount  # type: ignore[attr-defined]
