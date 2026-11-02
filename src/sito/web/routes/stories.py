"""Лента сюжетов с вкладками и страница сюжета.

Вкладки одной ленты: «Мои темы» (все темы пользователя), отдельные темы, «Главное» (сюжеты,
которые подхватили много изданий) и «Все новости». Адрес: /?view=my|top|all или /?topic=<id>.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sito.config import Settings
from sito.models import Topic, User
from sito.repositories.stories import FeedStory, StoryRepository
from sito.repositories.topics import TopicReason, TopicRepository
from sito.services.reading import ReadingService
from sito.services.search import SearchService
from sito.web.deps import CurrentUser, DbSession
from sito.web.formatting import day_label
from sito.web.templating import templates

router = APIRouter()

# Последняя открытая вкладка ленты (в сессии): на неё ведут главная и «← Лента» со страницы сюжета
LAST_FEED_KEY = "feed"

SUGGESTIONS_DAYS = 7
# После какой по счёту карточки первой страницы темы спросить «Подходит?» о спорных сюжетах
ASK_AFTER = 3


def _cursor(story: FeedStory) -> str:
    """Курсор для «Показать ещё»: время первого поста в микросекундах и id сюжета."""
    return f"{int(story.started_at.timestamp() * 1_000_000)}-{story.id}"


def _parse_cursor(raw: str | None) -> tuple[datetime, int] | None:
    if not raw:
        return None
    try:
        micros, story_id = raw.split("-", 1)
        return datetime.fromtimestamp(int(micros) / 1_000_000, UTC), int(story_id)
    except ValueError:
        return None  # испорченный курсор — просто первая страница


def _divider_before(stories: list[FeedStory], boundary: datetime | None, after: datetime | None) -> int | None:
    """id сюжета, перед которым показать «Вы были здесь»: первый сюжет не новее прошлого визита.

    after — время последнего сюжета предыдущей порции (для «Показать ещё»), чтобы разделитель
    встал и на границе порций. Если нового с прошлого визита нет совсем, разделитель не нужен.
    """
    if boundary is None:
        return None
    previous = after
    for story in stories:
        if story.started_at <= boundary:
            return story.id if previous is not None and previous > boundary else None
        previous = story.started_at
    return None


def _days_before(stories: list[FeedStory], user: User, now: datetime, after: datetime | None) -> dict[int, str]:
    """Заголовки дней: id сюжета → «Сегодня», «Вчера», «28 октября» перед первым сюжетом дня."""
    labels: dict[int, str] = {}
    previous = day_label(after, user.timezone, now) if after else None
    for story in stories:
        label = day_label(story.started_at, user.timezone, now)
        if label != previous:
            labels[story.id] = label
        previous = label
    return labels


@dataclass(frozen=True, slots=True)
class FeedView:
    """Какая вкладка открыта: my — все темы, topic — одна тема, top — главное, all — все новости."""

    kind: str
    topic_id: int | None = None

    @classmethod
    def parse(cls, view: str | None, topic: int | None, topics: list[Topic]) -> "FeedView":
        known = {t.id for t in topics}
        if topic is not None and topic in known:
            return cls("topic", topic)
        if view in ("my", "top", "all"):
            return cls(view) if view != "my" or topics else cls("top")
        # По умолчанию — свои темы, а пока их нет — главное
        return cls("my" if topics else "top")

    def query(self, **extra: object) -> str:
        params: dict[str, object] = {"topic": self.topic_id} if self.kind == "topic" else {"view": self.kind}
        params.update({k: v for k, v in extra.items() if v is not None})
        return urlencode(params)

    def url(self, path: str = "/", **extra: object) -> str:
        return f"{path}?{self.query(**extra)}"


async def _feed_context(
    request: Request, db: DbSession, user: User, view: FeedView, topics: list[Topic], cursor: str | None,
) -> dict[str, object]:
    config: Settings = request.app.state.settings
    now = datetime.now(UTC)
    before = _parse_cursor(cursor)
    active = [t for t in topics if not t.paused]
    topic_ids: list[int] | None = None
    if view.kind == "my":
        topic_ids = [t.id for t in active]
    elif view.kind == "topic":
        topic_ids = [view.topic_id] if view.topic_id is not None else []
    min_sources = config.top_min_sources if view.kind == "top" else 1

    # На один больше, чем показываем: так понятно, есть ли что-то дальше
    stories = await StoryRepository(db).feed(
        config.feed_page_size + 1, user_id=user.id, read_all_before=user.read_all_before,
        min_sources=min_sources, before=before, topic_ids=topic_ids,
    )
    has_more = len(stories) > config.feed_page_size
    stories = stories[: config.feed_page_size]
    next_cursor = _cursor(stories[-1]) if has_more else None
    reasons: dict[int, list[TopicReason]] = {}
    if topic_ids:
        reasons = await TopicRepository(db).reasons(user.id, [s.id for s in stories], topic_ids=topic_ids)

    # Подсказки темы: спорные сюжеты спрашиваются прямо в ленте («Подходит?»), остальные — в её конце
    ask: list[FeedStory] = []
    suggestions: list[FeedStory] = []
    suggestion_reasons: dict[int, list[TopicReason]] = {}
    topic = next((t for t in topics if t.id == view.topic_id), None)
    first_page = before is None
    if topic is not None and not topic.paused and (first_page or not has_more):
        ask, suggestions, suggestion_reasons = await _suggestions(request, db, user, topic)
        if not first_page or len(stories) <= ASK_AFTER:
            ask = []  # вопросы — только среди карточек первой страницы; на следующих они уже были
        if has_more:
            suggestions = []

    after = before[0] if before else None
    return {
        "stories": stories,
        "view": view,
        "reasons": reasons,
        "more_url": view.url(cursor=next_cursor) if next_cursor else None,
        "more_items_url": view.url("/feed/items", cursor=next_cursor) if next_cursor else None,
        "divider_before": _divider_before(stories, user.previous_visit_at, after),
        "days_before": _days_before(stories, user, now, after),
        "suggestions": suggestions,
        "suggestion_reasons": suggestion_reasons,
        "ask": ask,
        "ask_after": stories[ASK_AFTER - 1].id if ask else None,
        "ask_topic": topic,
        # С какого момента плашка «Ещё N новых» считает новое: самый свежий показанный сюжет
        "since": int(stories[0].started_at.timestamp() * 1_000_000) if stories and before is None else None,
        "now": now,
    }


async def _suggestions(
    request: Request, db: DbSession, user: User, topic: Topic,
) -> tuple[list[FeedStory], list[FeedStory], dict[int, list[TopicReason]]]:
    """Свежие сюжеты не из темы, похожие на неё: (спросить в ленте, «Ещё может подойти», причины).

    Спрашиваются самые спорные: чуть не дотянули до порога темы, но не дальше нижней границы,
    до которой порог может опуститься. Ответ на них сильнее всего подстраивает тему.
    """
    config: Settings = request.app.state.settings
    repo = TopicRepository(db)
    vector = None
    if topic.query:
        vector = await SearchService(db, request.app.state.embedder, config.search_limit).embed_query(topic.query)
    found = await repo.suggestions(
        topic, datetime.now(UTC) - timedelta(days=SUGGESTIONS_DAYS), config.topic_suggestions_limit,
        dislike_min_similarity=config.topic_dislike_min_similarity, query_embedding=vector,
    )
    ask_ids = [
        f.story_id for f in found if f.by_examples and f.similarity >= config.topic_similarity_floor
    ][: config.topic_ask_inline]
    ids = [f.story_id for f in found]
    cards = {c.id: c for c in await StoryRepository(db).cards(ids, user_id=user.id, read_all_before=user.read_all_before)}
    ask = [cards[i] for i in ask_ids if i in cards]
    rest = [cards[i] for i in ids if i in cards and i not in ask_ids]
    return ask, rest, await repo.reasons(user.id, ids, topic_ids=[topic.id], include_unmatched=True)


async def _tabs(db: DbSession, user: User, topics: list[Topic]) -> dict[str, object]:
    """Счётчики новых с прошлого визита на вкладках тем."""
    if user.previous_visit_at is None:
        return {"new_counts": {}, "new_total": 0}
    counts, total = await TopicRepository(db).new_counts(user.id, user.previous_visit_at)
    return {"new_counts": counts, "new_total": total}


@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request, db: DbSession, user: CurrentUser,
    view: str | None = None, topic: int | None = None, cursor: str | None = None,
) -> HTMLResponse:
    if cursor is None:  # заход в ленту, а не следующая страница
        ReadingService.register_visit(user)
        await db.commit()
    topics = await TopicRepository(db).list_for_user(user.id)
    if view is None and topic is None:
        # Просто «/» — открыть ту вкладку, где пользователь был в прошлый раз
        last = parse_qs(str(request.session.get(LAST_FEED_KEY, "")))
        view = last["view"][0] if last.get("view") else None
        topic_raw = (last.get("topic") or [""])[0]
        topic = int(topic_raw) if topic_raw.isdigit() else None
    feed_view = FeedView.parse(view, topic, topics)
    request.session[LAST_FEED_KEY] = feed_view.query()
    context = await _feed_context(request, db, user, feed_view, topics, cursor)
    return templates.TemplateResponse(request, "feed.html", {
        "user": user, "topics": topics, **await _tabs(db, user, topics), **context,
    })


@router.get("/feed/items", response_class=HTMLResponse)
async def feed_items(
    request: Request, db: DbSession, user: CurrentUser,
    view: str | None = None, topic: int | None = None, cursor: str | None = None,
) -> HTMLResponse:
    """Следующая порция карточек для HTMX: дописывается в конец ленты."""
    topics = await TopicRepository(db).list_for_user(user.id)
    context = await _feed_context(request, db, user, FeedView.parse(view, topic, topics), topics, cursor)
    return templates.TemplateResponse(request, "_feed_items.html", {"user": user, **context})


@router.get("/feed/newer", response_class=HTMLResponse)
async def feed_newer(
    request: Request, db: DbSession, user: CurrentUser,
    since: str, view: str | None = None, topic: int | None = None,
) -> HTMLResponse:
    """Новые сюжеты для начала ленты: лента раз в минуту спрашивает их и вставляет сама.

    Вместе с карточками приходит обновлённый «опросчик» (hx-swap-oob) с новой отметкой since.
    """
    config: Settings = request.app.state.settings
    moment = _parse_cursor(f"{since}-0")
    topics = await TopicRepository(db).list_for_user(user.id)
    feed_view = FeedView.parse(view, topic, topics)
    if moment is None:
        return HTMLResponse("")
    topic_ids = (
        [t.id for t in topics if not t.paused] if feed_view.kind == "my"
        else [feed_view.topic_id] if feed_view.topic_id is not None else None
    )
    stories = await StoryRepository(db).feed(
        config.feed_page_size, user_id=user.id, read_all_before=user.read_all_before,
        min_sources=config.top_min_sources if feed_view.kind == "top" else 1,
        topic_ids=topic_ids, after=moment[0],
    )
    if not stories:
        return HTMLResponse("")
    reasons = (
        await TopicRepository(db).reasons(user.id, [s.id for s in stories], topic_ids=topic_ids)
        if topic_ids else {}
    )
    return templates.TemplateResponse(request, "_newer.html", {
        "user": user, "stories": stories, "reasons": reasons, "view": feed_view,
        "since": int(stories[0].started_at.timestamp() * 1_000_000), "now": datetime.now(UTC),
    })


@router.get("/my")
async def my_feed_redirect(topic: int | None = None) -> Response:
    """Старый адрес персональной ленты: теперь это вкладки главной страницы."""
    return RedirectResponse(f"/?topic={topic}" if topic else "/?view=my", status.HTTP_301_MOVED_PERMANENTLY)


@router.get("/stories/{story_id}", response_class=HTMLResponse)
async def story_page(request: Request, db: DbSession, user: CurrentUser, story_id: int) -> HTMLResponse:
    config: Settings = request.app.state.settings
    repo = StoryRepository(db)
    story = await repo.get(story_id)
    if story is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сюжет не найден")
    timeline = await repo.timeline(story_id)
    similar = await repo.similar(
        story_id,
        min_similarity=config.related_min_similarity,
        window=timedelta(hours=config.related_window_hours),
        limit=config.related_limit,
    )
    # Темы, где сюжет есть или откуда пользователь его убрал (чтобы можно было вернуть)
    reasons = [
        r for r in (await TopicRepository(db).reasons(user.id, [story_id], include_unmatched=True)).get(story_id, [])
        if r.matched or r.vote
    ]
    await ReadingService(db).mark_read(user, story_id)
    await db.commit()
    # Назад — на ту вкладку, откуда пришли, к карточке этого сюжета
    back_url = f"/?{request.session.get(LAST_FEED_KEY, '')}#story-{story_id}"
    return templates.TemplateResponse(request, "story.html", {
        "user": user, "story": story, "timeline": timeline, "similar": similar,
        "reasons": reasons, "next": f"/stories/{story_id}", "back_url": back_url, "now": datetime.now(UTC),
    })
