"""Шаги 5–6 пайплайна: склеить пост с сюжетом или начать новый, пересчитать сводку сюжета.

Склеиваем только уверенные пары (пороги подобраны по разметке в eval/). Всё спорное
становится отдельным сюжетом и показывается рядом как «похожее» — так ошибочная склейка
не спрячет новость. LLM для спорной зоны можно добавить позже, не меняя остального.

Работает в одном процессе: два параллельных обработчика могли бы одновременно создать
два сюжета для одного события.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Post, PostStatus
from sito.repositories.posts import PostRepository
from sito.repositories.stories import MatchCandidate, StoryRepository
from sito.services.pipeline import mark_post_done, record_post_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClusteringRules:
    cross_source_min_similarity: float = 0.90  # разные издания
    same_source_min_similarity: float = 0.94   # одно издание: похожие, но разные новости там чаще
    max_gap: timedelta = timedelta(hours=6)    # однотипные новости разных дней не склеиваем

    def accepts(self, post: Post, candidate: MatchCandidate) -> bool:
        same_source = candidate.source_id == post.source_id
        threshold = self.same_source_min_similarity if same_source else self.cross_source_min_similarity
        return candidate.similarity >= threshold


@dataclass(frozen=True, slots=True)
class ClusterBatchResult:
    attached: int = 0     # приклеены к существующему сюжету
    new_stories: int = 0
    errors: int = 0

    @property
    def total(self) -> int:
        return self.attached + self.new_stories + self.errors

    def __str__(self) -> str:
        return f"в сюжеты={self.attached} новых сюжетов={self.new_stories} ошибок={self.errors}"


class ClusteringService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        rules: ClusteringRules | None = None,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.rules = rules or ClusteringRules()
        self.batch_size = batch_size

    async def process_batch(self) -> ClusterBatchResult:
        attached = new_stories = errors = 0
        async with self.session_factory() as session, session.begin():
            stories = StoryRepository(session)
            posts = await PostRepository(session).take_pending(PostStatus.EMBEDDED, self.batch_size)
            for post in posts:
                post_id = post.id
                try:
                    # SAVEPOINT: ошибка одного поста не откатывает уже склеенные посты пачки
                    async with session.begin_nested():
                        is_new = await self._cluster(stories, post)
                except Exception as exc:
                    errors += 1
                    logger.exception("post=%s: ошибка склейки", post_id, extra={"post_id": post_id})
                    # После отката SAVEPOINT изменённый объект устарел: неявная загрузка в async
                    # невозможна (MissingGreenlet), поэтому перечитываем явно
                    await session.refresh(post)
                    record_post_error(post, exc)
                else:
                    new_stories += is_new
                    attached += not is_new

        result = ClusterBatchResult(attached, new_stories, errors)
        if result.total:
            logger.info("склейка: %s", result, extra=asdict(result))
        return result

    async def _cluster(self, stories: StoryRepository, post: Post) -> bool:
        """Привязать пост к сюжету. Возвращает True, если сюжет новый."""
        if post.embedding is None:
            raise ValueError("у поста нет вектора")
        event_at = min(post.published_at or post.fetched_at, post.fetched_at)

        is_new = False
        if post.story_id is None:
            candidates = await stories.match_candidates(post.id, post.embedding, event_at, self.rules.max_gap)
            match = next((c for c in candidates if self.rules.accepts(post, c)), None)
            if match:
                post.story_id = match.story_id
            else:
                post.story_id = (await stories.create(post.title, event_at)).id
                is_new = True
        # Если story_id уже есть, пост отредактировали: он остаётся в своём сюжете,
        # а сводку сюжета (заголовок, счётчики) нужно пересчитать.

        await stories.session.flush()
        await stories.refresh(post.story_id)
        mark_post_done(post, PostStatus.CLUSTERED)
        return is_new
