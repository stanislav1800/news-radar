"""Шаги 5–6 пайплайна: склеить пост с сюжетом или начать новый, пересчитать сводку сюжета.

Склеиваем только уверенные пары (пороги подобраны по разметке в eval/). Всё спорное
становится отдельным сюжетом и показывается рядом как «похожее» — так ошибочная склейка
не спрячет новость. LLM для спорной зоны можно добавить позже, не меняя остального.

Склеивает один процесс за раз: это обеспечивает блокировка в базе (advisory lock),
иначе два обработчика могли бы одновременно создать два сюжета для одного события.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.config import Settings
from sito.models import Post, PostStatus
from sito.repositories.posts import PostRepository
from sito.repositories.stories import MatchCandidate, StoryRepository
from sito.services.pipeline import mark_post_done, record_post_error

logger = logging.getLogger(__name__)

# Ключ блокировки «склеивает только один процесс» (pg_advisory_xact_lock): любое постоянное число
CLUSTERING_LOCK_KEY = 5_172_640_001


@dataclass(frozen=True, slots=True)
class ClusteringRules:
    cross_source_min_similarity: float = 0.90  # разные издания
    same_source_min_similarity: float = 0.94   # одно издание: похожие, но разные новости там чаще
    max_gap: timedelta = timedelta(hours=6)    # однотипные новости разных дней не склеиваем
    # Пост должен быть похож и на центр всего сюжета, а не только на ближайший пост в нём.
    # Иначе крупный сюжет «расползается» цепочкой: атака на Москву → итоги по России → Курская область.
    min_story_similarity: float = 0.92
    # Ссылка на статью другого издания — то же событие, если они не дальше этого по времени
    link_max_gap: timedelta = timedelta(hours=24)
    # Страховка для ссылок: совсем непохожий пост не клеим даже по ссылке (в замере все пары ≥ 0.85)
    link_min_similarity: float = 0.80

    @classmethod
    def from_settings(cls, config: Settings) -> "ClusteringRules":
        return cls(
            cross_source_min_similarity=config.cluster_cross_source_min_similarity,
            same_source_min_similarity=config.cluster_same_source_min_similarity,
            max_gap=timedelta(hours=config.cluster_max_gap_hours),
            min_story_similarity=config.cluster_min_story_similarity,
            link_max_gap=timedelta(hours=config.cluster_link_max_gap_hours),
        )

    def accepts(self, post: Post, candidate: MatchCandidate) -> bool:
        same_source = candidate.source_id == post.source_id
        threshold = self.same_source_min_similarity if same_source else self.cross_source_min_similarity
        if candidate.similarity < threshold:
            return False
        return candidate.story_similarity is None or candidate.story_similarity >= self.min_story_similarity


@dataclass(frozen=True, slots=True)
class ClusterBatchResult:
    attached: int = 0     # приклеены к существующему сюжету
    new_stories: int = 0
    errors: int = 0
    by_link: int = 0      # из приклеенных — по ссылке

    @property
    def total(self) -> int:
        return self.attached + self.new_stories + self.errors

    def __str__(self) -> str:
        return (
            f"в сюжеты={self.attached} (по ссылке={self.by_link}) новых сюжетов={self.new_stories} "
            f"ошибок={self.errors}"
        )


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
        attached = new_stories = errors = by_link = 0
        async with self.session_factory() as session, session.begin():
            # Другой процесс сейчас склеивает — пропускаем круг, а не склеиваем параллельно
            if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": CLUSTERING_LOCK_KEY}):
                return ClusterBatchResult()
            stories = StoryRepository(session)
            # По времени новости: так сюжет растёт в том порядке, в каком событие развивалось
            posts = await PostRepository(session).take_pending(PostStatus.EMBEDDED, self.batch_size, by_event_time=True)
            for post in posts:
                post_id = post.id
                try:
                    # SAVEPOINT: ошибка одного поста не откатывает уже склеенные посты пачки
                    async with session.begin_nested():
                        how = await self._cluster(stories, post)
                except Exception as exc:
                    errors += 1
                    logger.exception("post=%s: ошибка склейки", post_id, extra={"post_id": post_id})
                    # После отката SAVEPOINT изменённый объект устарел: неявная загрузка в async
                    # невозможна (MissingGreenlet), поэтому перечитываем явно
                    await session.refresh(post)
                    record_post_error(post, exc)
                else:
                    new_stories += how == "new"
                    attached += how != "new"
                    by_link += how == "link"

        result = ClusterBatchResult(attached, new_stories, errors, by_link)
        if result.total:
            logger.info("склейка: %s", result, extra=asdict(result))
        return result

    async def _cluster(self, stories: StoryRepository, post: Post) -> str:
        """Привязать пост к сюжету. Возвращает, как: link, similar, new или edited."""
        if post.embedding is None:
            raise ValueError("у поста нет вектора")
        event_at = min(post.published_at or post.fetched_at, post.fetched_at)

        how = "edited"
        if post.story_id is None:
            # Ссылка между изданиями — самый надёжный признак: проверяем её первой
            linked = await stories.link_candidates(
                post.id, post.source_id, post.url_normalized, post.links, post.embedding,
                event_at, self.rules.link_max_gap,
            )
            link = next((c for c in linked if c.similarity >= self.rules.link_min_similarity), None)
            if link:
                post.story_id, how = link.story_id, "link"
            else:
                candidates = await stories.match_candidates(post.id, post.embedding, event_at, self.rules.max_gap)
                match = next((c for c in candidates if self.rules.accepts(post, c)), None)
                if match:
                    post.story_id, how = match.story_id, "similar"
                else:
                    post.story_id, how = (await stories.create(post.title, event_at)).id, "new"
        # Если story_id уже есть, пост отредактировали: он остаётся в своём сюжете,
        # а сводку сюжета (заголовок, счётчики) нужно пересчитать.

        await stories.session.flush()
        await stories.refresh(post.story_id)
        if how != "edited":
            post.clustered_at = func.clock_timestamp()
        mark_post_done(post, PostStatus.CLUSTERED)
        return how

    async def reset(self) -> int:
        """Снять со всех постов привязку к сюжетам и удалить сюжеты, чтобы склеить заново.

        Ждёт, пока другой процесс закончит текущую пачку. Возвращает число постов к пересклейке.
        """
        async with self.session_factory() as session, session.begin():
            await self._lock_for_bulk(session)
            return await StoryRepository(session).reset_all()

    async def reprocess(self, source_type: str, *, untitled: bool = False) -> int:
        """Отправить посты источников типа заново через конвейер (см. StoryRepository.reprocess).

        Под той же блокировкой, что и склейка: иначе worker мог бы приклеить пост к сюжету, который
        reprocess тут же удалит, — пост остался бы «склеенным» без сюжета и пропал из ленты.
        """
        async with self.session_factory() as session, session.begin():
            await self._lock_for_bulk(session)
            return await StoryRepository(session).reprocess(source_type, untitled=untitled)

    @staticmethod
    async def _lock_for_bulk(session: AsyncSession) -> None:
        """Дождаться, пока worker допишет пачку, и снять лимит времени запроса: на полной базе
        массовый UPDATE и удаление сюжетов дольше обычных 300 с, а откат по таймауту не нужен никому."""
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLUSTERING_LOCK_KEY})
        await session.execute(text("SET LOCAL statement_timeout = 0"))
