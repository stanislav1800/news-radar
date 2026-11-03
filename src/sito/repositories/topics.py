from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, bindparam, case, delete, func, literal, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sito.models import Story, Topic, TopicExample, TopicMatch
from sito.models.post import EMBEDDING_DIM, STORY_HAS_TEXT_SQL
from sito.utils.lemmas import parse_query


@dataclass(frozen=True, slots=True)
class MatchResult:
    added: int                     # сюжетов впервые попало в тему
    removed: int                   # сюжетов перестало подходить
    last_updated: datetime | None  # самый поздний story.updated_at среди просмотренных сюжетов


@dataclass(frozen=True, slots=True)
class TopicReason:
    """Почему сюжет в теме (или не в ней) и как пользователь его отметил."""

    topic_id: int
    topic_name: str
    story_id: int
    matched: bool
    by_words: bool
    example_title: str | None  # на какой 👍-пример похож
    vote: str | None           # like, dislike или None
    words: tuple[str, ...] = ()  # слова запроса темы в начальной форме: «есть слова: уголь, кузбасс»


@dataclass(frozen=True, slots=True)
class TopicStats:
    topic: Topic
    matches_week: int
    likes: int
    dislikes: int


# Пост подходит к словам темы: в нём есть все слова запроса. Если запрос — одно слово, строже:
# слово в заголовке или хотя бы дважды в тексте. Иначе тема «золото» ловит статью, где «золотой»
# мелькнул один раз (замер 2.11: так в «золото» и «уголь» попадали программирование и отключения света)
_WORD_MATCH_SQL = """
    post.search_vector @@ to_tsquery('simple', :words)
    AND (:title_words = ''
         OR post.search_vector @@ to_tsquery('simple', :title_words)
         OR EXISTS (
             SELECT 1 FROM unnest(CAST(:single_words AS text[])) AS w
             WHERE regexp_count(coalesce(post.text_lemmas, ''), '(^| )' || w || '(?= |$)') >= 2
         ))
"""

_PREVIEW_SQL = text(f"""
    SELECT count(DISTINCT post.story_id) FROM post JOIN story ON story.id = post.story_id
    WHERE story.last_post_at >= :since AND {_WORD_MATCH_SQL}
""")


def _source_params(topic: Topic) -> dict[str, object]:
    return {"sources": list(topic.source_ids or []), "all_sources": not topic.source_ids}


def _word_params(query: str | None) -> dict[str, object]:
    """Параметры _WORD_MATCH_SQL: все слова запроса в любой форме («уголь в кузбассе» → уголь & кузбасс)."""
    parsed = parse_query(query) if query else None
    return {
        "words": (parsed.tsquery_all() or "") if parsed else "",
        "title_words": (parsed.tsquery_title() or "") if parsed else "",
        "single_words": (parsed.single_word or []) if parsed else [],
    }


# У темы могут быть свои источники (тема «IT» — только Хабр и OpenNET): сюжет подходит, если в нём есть
# пост из них. Добавленные вручную (👍) сюжеты остаются в теме в любом случае
_IN_TOPIC_SOURCES_SQL = (
    "(:all_sources OR EXISTS (SELECT 1 FROM post AS p WHERE p.story_id = story.id AND p.source_id = ANY(:sources)))"
)
_SOURCES_PARAM = bindparam("sources", type_=ARRAY(BigInteger))

# Правило совпадения (шаг 7). Сюжет подходит к теме, если:
#   — пользователь сам отметил его 👍; или
#   — в нём есть все слова запроса (в любой форме, по леммам), или он похож на 👍-пример не меньше :min_similarity,
#     и при этом он не 👎 сам и не похож на 👎-пример (от :dislike_min_similarity) сильнее, чем на лучший 👍.
# Просматриваются сюжеты, изменённые после :updated_after и живые после :active_after.
# Для них совпадения пересчитываются: новые добавляются, переставшие подходить удаляются.
_REMATCH_SQL = text(f"""
    WITH ex AS (
        SELECT id, story_id, liked, embedding FROM topic_example WHERE topic_id = :topic_id
    ),
    cand AS (
        SELECT id, centroid FROM story
        WHERE centroid IS NOT NULL AND updated_at > :updated_after AND last_post_at >= :active_after
          AND ({_IN_TOPIC_SOURCES_SQL} OR story.id IN (SELECT story_id FROM ex WHERE liked))
    ),
    words AS (
        SELECT DISTINCT post.story_id FROM post JOIN cand ON cand.id = post.story_id
        WHERE :words <> '' AND {_WORD_MATCH_SQL} AND (:all_sources OR post.source_id = ANY(:sources))
    ),
    scored AS (
        SELECT cand.id AS story_id,
               pos.id AS example_id,
               pos.similarity,
               neg.similarity AS neg_similarity,
               cand.id IN (SELECT story_id FROM words) AS by_words,
               cand.id IN (SELECT story_id FROM ex WHERE liked) AS liked,
               cand.id IN (SELECT story_id FROM ex WHERE NOT liked) AS disliked
        FROM cand
        LEFT JOIN LATERAL (
            SELECT ex.id, 1 - (ex.embedding <=> cand.centroid) AS similarity FROM ex
            WHERE ex.liked ORDER BY ex.embedding <=> cand.centroid LIMIT 1
        ) AS pos ON true
        LEFT JOIN LATERAL (
            SELECT max(1 - (ex.embedding <=> cand.centroid)) AS similarity FROM ex WHERE NOT ex.liked
        ) AS neg ON true
    ),
    decided AS (
        SELECT *, liked OR (
            NOT disliked
            AND (by_words OR coalesce(similarity, -1) >= :min_similarity)
            AND NOT (coalesce(neg_similarity, -1) >= :dislike_min_similarity
                     AND coalesce(neg_similarity, -1) > coalesce(similarity, -1))
        ) AS matched
        FROM scored
    ),
    removed AS (
        DELETE FROM topic_match USING decided
        WHERE topic_match.topic_id = :topic_id AND topic_match.story_id = decided.story_id AND NOT decided.matched
        RETURNING 1
    ),
    upserted AS (
        INSERT INTO topic_match (topic_id, story_id, by_words, example_id, similarity, matched_at)
        SELECT :topic_id, story_id, by_words,
               CASE WHEN coalesce(similarity, -1) >= :min_similarity THEN example_id END,
               similarity, clock_timestamp()
        FROM decided WHERE matched
        ON CONFLICT (topic_id, story_id) DO UPDATE SET
            by_words = excluded.by_words, example_id = excluded.example_id, similarity = excluded.similarity
        WHERE (topic_match.by_words, topic_match.example_id, topic_match.similarity)
              IS DISTINCT FROM (excluded.by_words, excluded.example_id, excluded.similarity)
        RETURNING xmax = 0 AS inserted
    )
    SELECT (SELECT count(*) FROM upserted WHERE inserted) AS added,
           (SELECT count(*) FROM removed) AS removed,
           (SELECT max(updated_at) FROM story WHERE updated_at > :updated_after) AS last_updated
""").bindparams(_SOURCES_PARAM)

# «Ещё может подойти»: свежие сюжеты не из темы, ближайшие к 👍-примерам (или к запросу, пока примеров нет).
# Похожие на 👎-пример сильнее, чем на 👍, не предлагаются: «Не подходит» на подсказке убирает и такие
_NEGATIVE_SQL = """
    CROSS JOIN LATERAL (
        SELECT max(1 - (ex.embedding <=> story.centroid)) AS similarity FROM topic_example AS ex
        WHERE ex.topic_id = :topic_id AND NOT ex.liked
    ) AS neg
"""
_FRESH_NOT_IN_TOPIC_SQL = f"""
    {_IN_TOPIC_SOURCES_SQL} AND story.centroid IS NOT NULL AND story.last_post_at >= :since
      AND story.id NOT IN (SELECT story_id FROM topic_match WHERE topic_id = :topic_id)
      AND story.id NOT IN (SELECT story_id FROM topic_example WHERE topic_id = :topic_id AND story_id IS NOT NULL)
"""

# Опоры подсказок: 👍-примеры темы, а пока их нет — сюжеты, найденные по словам (последние WORD_ANCHORS).
# Похожие на найденное по словам в тему сами не попадают: замер на живых данных (2.11) показал, что мусор
# лежит к ним так же близко, как нужное (у «золота» реставрация храма — 0.95). Поэтому только как вопрос
_SUGGESTIONS_BY_ANCHORS_SQL = text(f"""
    WITH anchor AS (
        SELECT embedding AS v FROM topic_example WHERE topic_id = :topic_id AND liked AND NOT :by_words
        UNION ALL
        (SELECT story.centroid FROM topic_match JOIN story ON story.id = topic_match.story_id
         WHERE :by_words AND topic_match.topic_id = :topic_id AND topic_match.by_words
           AND story.centroid IS NOT NULL
         ORDER BY story.started_at DESC LIMIT :word_anchors)
    )
    SELECT story.id, 1 - nearest.distance AS similarity FROM story
    CROSS JOIN LATERAL (SELECT min(anchor.v <=> story.centroid) AS distance FROM anchor) AS nearest
    {_NEGATIVE_SQL}
    WHERE {_FRESH_NOT_IN_TOPIC_SQL} AND nearest.distance IS NOT NULL
      AND NOT (coalesce(neg.similarity, -1) >= :dislike_min_similarity
               AND neg.similarity > 1 - nearest.distance)
    ORDER BY nearest.distance
    LIMIT :limit
""").bindparams(_SOURCES_PARAM)

_SUGGESTIONS_BY_QUERY_SQL = text(f"""
    SELECT story.id, 1 - (story.centroid <=> :embedding) AS similarity FROM story
    {_NEGATIVE_SQL}
    WHERE {_FRESH_NOT_IN_TOPIC_SQL} AND {STORY_HAS_TEXT_SQL}
      AND coalesce(neg.similarity, -1) < :dislike_min_similarity
    ORDER BY story.centroid <=> :embedding
    LIMIT :limit
""").bindparams(bindparam("embedding", type_=Vector(EMBEDDING_DIM)), _SOURCES_PARAM)

# Насколько каждый пример темы похож на другие 👍-примеры: по этому подстраивается порог темы
_EXAMPLE_SIMILARITIES_SQL = text("""
    SELECT ex.liked, (
        SELECT max(1 - (other.embedding <=> ex.embedding)) FROM topic_example AS other
        WHERE other.topic_id = ex.topic_id AND other.liked AND other.id <> ex.id
    ) AS similarity
    FROM topic_example AS ex WHERE ex.topic_id = :topic_id
""")


# Сколько последних найденных по словам сюжетов берётся опорами подсказок
WORD_ANCHORS = 50


@dataclass(frozen=True, slots=True)
class Suggestion:
    story_id: int
    similarity: float
    # С чем сравнивали: examples — 👍-примеры, words — найденное по словам, query — текст запроса.
    # Похожесть на текст запроса другая по масштабу: короткая фраза против целого сюжета
    basis: str


class TopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, topic_id: int) -> Topic | None:
        return await self.session.get(Topic, topic_id)

    async def get_owned(self, topic_id: int, user_id: int, *, lock: bool = False) -> Topic | None:
        """Тема пользователя. Чужая тема — как несуществующая.

        lock — заблокировать строку до конца транзакции: изменения примеров и пересчёт совпадений
        одной темы идут по очереди, иначе worker мог бы записать совпадения по старым примерам.
        """
        query = select(Topic).where(Topic.id == topic_id, Topic.user_id == user_id)
        if lock:
            query = query.with_for_update()
        return await self.session.scalar(query)

    async def lock_for_matching(self, topic_id: int) -> Topic | None:
        """Для worker'а: заблокировать активную тему, если её прямо сейчас не меняет веб."""
        return await self.session.scalar(
            select(Topic).where(Topic.id == topic_id, Topic.paused.is_(False))
            .with_for_update(skip_locked=True)
        )

    async def by_rss_token(self, token: str) -> Topic | None:
        return await self.session.scalar(select(Topic).where(Topic.rss_token == token))

    async def list_for_user(self, user_id: int, *, active_only: bool = False) -> list[Topic]:
        query = select(Topic).where(Topic.user_id == user_id).order_by(func.lower(Topic.name), Topic.id)
        if active_only:
            query = query.where(Topic.paused.is_(False))
        return list(await self.session.scalars(query))

    async def stats(self, user_id: int, since: datetime) -> list[TopicStats]:
        """Темы пользователя со счётчиками: совпадений с since, 👍 и 👎."""
        matches = (
            select(func.count()).select_from(TopicMatch).join(Story, Story.id == TopicMatch.story_id)
            .where(TopicMatch.topic_id == Topic.id, Story.last_post_at >= since)
            .scalar_subquery()
        )
        likes = (
            select(func.count(case((TopicExample.liked, 1))))
            .where(TopicExample.topic_id == Topic.id).scalar_subquery()
        )
        dislikes = (
            select(func.count(case((TopicExample.liked.is_(False), 1))))
            .where(TopicExample.topic_id == Topic.id).scalar_subquery()
        )
        query = (
            select(Topic, matches, likes, dislikes)
            .where(Topic.user_id == user_id)
            .order_by(func.lower(Topic.name), Topic.id)
        )
        return [TopicStats(*row) for row in await self.session.execute(query)]

    async def active_ids_behind(self) -> list[int]:
        """Активные темы, которые ещё не видели последних изменений сюжетов."""
        latest = select(func.max(Story.updated_at)).scalar_subquery()
        # Пока сюжетов нет совсем, latest — NULL, и сравнение ложно: worker не крутится вхолостую
        never = literal(datetime(1970, 1, 1, tzinfo=UTC))
        query = select(Topic.id).where(
            Topic.paused.is_(False), func.coalesce(Topic.matched_until, never) < latest
        ).order_by(Topic.id)
        return list(await self.session.scalars(query))

    def add(self, user_id: int, name: str, query: str | None, rss_token: str) -> Topic:
        topic = Topic(user_id=user_id, name=name, query=query, rss_token=rss_token)
        self.session.add(topic)
        return topic

    async def examples(self, topic_id: int) -> list[TopicExample]:
        return list(await self.session.scalars(
            select(TopicExample).where(TopicExample.topic_id == topic_id).order_by(TopicExample.created_at.desc())
        ))

    async def example_for_story(self, topic_id: int, story_id: int) -> TopicExample | None:
        return await self.session.scalar(
            select(TopicExample).where(TopicExample.topic_id == topic_id, TopicExample.story_id == story_id)
        )

    async def delete_example(self, topic_id: int, example_id: int) -> bool:
        result = await self.session.execute(
            delete(TopicExample).where(TopicExample.id == example_id, TopicExample.topic_id == topic_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def rematch(
        self, topic: Topic, *, updated_after: datetime, active_after: datetime,
        min_similarity: float, dislike_min_similarity: float,
    ) -> MatchResult:
        """Пересчитать совпадения темы для сюжетов, изменённых после updated_after."""
        row = (await self.session.execute(_REMATCH_SQL, {
            "topic_id": topic.id,
            **_word_params(topic.query),
            **_source_params(topic),
            "updated_after": updated_after,
            "active_after": active_after,
            "min_similarity": min_similarity,
            "dislike_min_similarity": dislike_min_similarity,
        })).one()
        return MatchResult(added=row.added, removed=row.removed, last_updated=row.last_updated)

    async def reasons(
        self, user_id: int, story_ids: Sequence[int], *, topic_ids: Sequence[int] | None = None,
        include_unmatched: bool = False,
    ) -> dict[int, list[TopicReason]]:
        """Почему сюжеты в темах пользователя: story_id → причины по каждой теме.

        include_unmatched — и темы, к которым сюжет не подошёл (чтобы показать, что 👎 сработал).
        """
        if not story_ids:
            return {}
        example = aliased(TopicExample)
        vote = aliased(TopicExample)
        query = (
            select(
                Topic.id.label("topic_id"), Topic.name.label("topic_name"), Topic.query.label("topic_query"),
                Story.id.label("story_id"),
                TopicMatch.story_id.is_not(None).label("matched"),
                func.coalesce(TopicMatch.by_words, False).label("by_words"),
                example.title.label("example_title"),
                case((vote.liked, "like"), (vote.liked.is_(False), "dislike")).label("vote"),
            )
            .select_from(Topic)
            .join(Story, Story.id.in_(story_ids))  # каждая тема × каждый сюжет
            .outerjoin(TopicMatch, (TopicMatch.topic_id == Topic.id) & (TopicMatch.story_id == Story.id))
            .outerjoin(example, example.id == TopicMatch.example_id)
            .outerjoin(vote, (vote.topic_id == Topic.id) & (vote.story_id == Story.id))
            .where(Topic.user_id == user_id, Topic.paused.is_(False))
            .order_by(func.lower(Topic.name), Topic.id)
        )
        if topic_ids is not None:
            query = query.where(Topic.id.in_(topic_ids))
        if not include_unmatched:
            query = query.where(TopicMatch.story_id.is_not(None))
        reasons: dict[int, list[TopicReason]] = {}
        for row in await self.session.execute(query):
            fields = dict(row._mapping)
            topic_query = fields.pop("topic_query")
            words = tuple(parse_query(topic_query).words) if topic_query else ()
            reasons.setdefault(row.story_id, []).append(TopicReason(**fields, words=words))
        return reasons

    async def suggestions(
        self, topic: Topic, since: datetime, limit: int, *, dislike_min_similarity: float,
        query_embedding: list[float] | None = None,
    ) -> list[Suggestion]:
        """Свежие сюжеты не из темы, которые на неё похожи: кандидаты в 👍, самые похожие первыми."""
        has_likes = await self.session.scalar(
            select(TopicExample.id).where(TopicExample.topic_id == topic.id, TopicExample.liked).limit(1)
        )
        params: dict[str, object] = {
            "topic_id": topic.id, "since": since, "limit": limit, "dislike_min_similarity": dislike_min_similarity,
            **_source_params(topic),
        }
        has_word_matches = not has_likes and await self.session.scalar(
            select(TopicMatch.story_id).where(TopicMatch.topic_id == topic.id, TopicMatch.by_words).limit(1)
        )
        if has_likes or has_word_matches:
            basis = "examples" if has_likes else "words"
            result = await self.session.execute(_SUGGESTIONS_BY_ANCHORS_SQL, {
                **params, "by_words": basis == "words", "word_anchors": WORD_ANCHORS,
            })
        elif query_embedding is not None:
            basis = "query"
            result = await self.session.execute(_SUGGESTIONS_BY_QUERY_SQL, {**params, "embedding": query_embedding})
        else:
            return []
        return [Suggestion(row.id, row.similarity, basis) for row in result]

    async def preview(self, query: str, since: datetime) -> int:
        """Сколько сюжетов с since тема с таким запросом нашла бы по словам: до того, как её создать."""
        params = _word_params(query)
        if not params["words"]:
            return 0
        return await self.session.scalar(_PREVIEW_SQL, {**params, "since": since}) or 0

    async def prune_by_sources(self, topic: Topic) -> None:
        """Источники темы изменились: убрать совпадения, где нет постов из её источников (кроме добавленных вручную)."""
        if not topic.source_ids:
            return
        await self.session.execute(text("""
            DELETE FROM topic_match AS m WHERE m.topic_id = :topic_id
              AND NOT EXISTS (SELECT 1 FROM post WHERE post.story_id = m.story_id AND post.source_id = ANY(:sources))
              AND m.story_id NOT IN (SELECT story_id FROM topic_example WHERE topic_id = :topic_id AND liked
                                     AND story_id IS NOT NULL)
        """).bindparams(_SOURCES_PARAM), {"topic_id": topic.id, "sources": list(topic.source_ids)})

    async def example_similarities(self, topic_id: int) -> tuple[list[float], list[float]]:
        """Похожесть каждого 👍 и каждого 👎 темы на остальные 👍-примеры (для подстройки порога)."""
        likes: list[float] = []
        dislikes: list[float] = []
        for liked, similarity in await self.session.execute(_EXAMPLE_SIMILARITIES_SQL, {"topic_id": topic_id}):
            if similarity is not None:
                (likes if liked else dislikes).append(similarity)
        return likes, dislikes

    async def new_counts(self, user_id: int, since: datetime) -> tuple[dict[int, int], int]:
        """Сколько сюжетов появилось в каждой активной теме после since и сколько всего (без повторов)."""
        query = (
            select(TopicMatch.topic_id, TopicMatch.story_id)
            .join(Story, Story.id == TopicMatch.story_id)
            .join(Topic, Topic.id == TopicMatch.topic_id)
            .where(Topic.user_id == user_id, Topic.paused.is_(False), Story.started_at > since)
        )
        per_topic: dict[int, int] = {}
        stories: set[int] = set()
        for topic_id, story_id in await self.session.execute(query):
            per_topic[topic_id] = per_topic.get(topic_id, 0) + 1
            stories.add(story_id)
        return per_topic, len(stories)

    async def matched_count(self, topic_id: int) -> int:
        return await self.session.scalar(
            select(func.count()).select_from(TopicMatch).where(TopicMatch.topic_id == topic_id)
        ) or 0

    async def matched_story_ids(self, topic_id: int, limit: int) -> list[int]:
        """Свежие сюжеты темы, для RSS: новые сверху."""
        return list(await self.session.scalars(
            select(TopicMatch.story_id).join(Story, Story.id == TopicMatch.story_id)
            .where(TopicMatch.topic_id == topic_id)
            .order_by(Story.started_at.desc(), Story.id.desc())
            .limit(limit)
        ))
