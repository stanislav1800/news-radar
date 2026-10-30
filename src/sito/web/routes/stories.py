"""Ленты сюжетов (общая и персональная) и страница сюжета."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sito.config import Settings
from sito.models import User
from sito.repositories.stories import FeedStory, StoryRepository
from sito.repositories.topics import TopicRepository
from sito.services.reading import ReadingService
from sito.web.deps import CurrentUser, DbSession
from sito.web.security import safe_next
from sito.web.templating import templates

router = APIRouter()

SOURCE_FILTERS = {1: "Все", 2: "2+ издания", 5: "5+"}


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


@dataclass(frozen=True, slots=True)
class FeedFilters:
    """Фильтры ленты. Общая лента — без темы; персональная — по всем темам или одной (topic)."""

    personal: bool = False
    topic: int | None = None
    min_sources: int = 1
    unread: bool = False

    def query(self, cursor: str | None = None) -> str:
        params: dict[str, object] = {}
        if self.personal and self.topic is not None:
            params["topic"] = self.topic
        if not self.personal and self.min_sources != 1:
            params["min_sources"] = self.min_sources
        if self.unread:
            params["unread"] = "true"
        if cursor:
            params["cursor"] = cursor
        return urlencode(params)

    def url(self, cursor: str | None = None, items: bool = False) -> str:
        path = ("/my" if self.personal else "/feed") + ("/items" if items else "")
        if path == "/feed":
            path = "/"
        query = self.query(cursor)
        return f"{path}?{query}" if query else path


async def _feed_context(
    db: DbSession, config: Settings, user: User, filters: FeedFilters, cursor: str | None,
) -> dict[str, object]:
    before = _parse_cursor(cursor)
    topic_ids: list[int] | None = None
    if filters.personal:
        active = await TopicRepository(db).list_for_user(user.id, active_only=True)
        topic_ids = [t.id for t in active if filters.topic in (None, t.id)]
    # На один больше, чем показываем: так понятно, есть ли что-то дальше
    stories = await StoryRepository(db).feed(
        config.feed_page_size + 1,
        user_id=user.id, read_all_before=user.read_all_before,
        min_sources=filters.min_sources, before=before, unread_only=filters.unread, topic_ids=topic_ids,
    )
    has_more = len(stories) > config.feed_page_size
    stories = stories[: config.feed_page_size]
    next_cursor = _cursor(stories[-1]) if has_more else None
    reasons = (
        await TopicRepository(db).reasons(user.id, [s.id for s in stories], topic_ids=topic_ids)
        if filters.personal else {}
    )
    return {
        "stories": stories,
        "filters": filters,
        "reasons": reasons,
        "next": filters.url(),
        "more_url": filters.url(next_cursor) if next_cursor else None,
        "more_items_url": filters.url(next_cursor, items=True) if next_cursor else None,
        "divider_before": _divider_before(stories, user.previous_visit_at, before[0] if before else None),
        "now": datetime.now(UTC),
    }


@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request, db: DbSession, user: CurrentUser,
    min_sources: int = 1, unread: bool = False, cursor: str | None = None,
) -> HTMLResponse:
    if cursor is None:  # заход в ленту, а не следующая страница
        ReadingService.register_visit(user)
        await db.commit()
    filters = FeedFilters(min_sources=min_sources if min_sources in SOURCE_FILTERS else 1, unread=unread)
    context = await _feed_context(db, request.app.state.settings, user, filters, cursor)
    return templates.TemplateResponse(
        request, "feed.html", {"user": user, "source_filters": SOURCE_FILTERS, **context}
    )


@router.get("/feed/items", response_class=HTMLResponse)
async def feed_items(
    request: Request, db: DbSession, user: CurrentUser,
    min_sources: int = 1, unread: bool = False, cursor: str | None = None,
) -> HTMLResponse:
    """Следующая порция карточек для HTMX: дописывается в конец ленты."""
    filters = FeedFilters(min_sources=min_sources if min_sources in SOURCE_FILTERS else 1, unread=unread)
    context = await _feed_context(db, request.app.state.settings, user, filters, cursor)
    return templates.TemplateResponse(request, "_feed_items.html", {"user": user, **context})


@router.get("/my", response_class=HTMLResponse)
async def my_feed(
    request: Request, db: DbSession, user: CurrentUser,
    topic: int | None = None, unread: bool = False, cursor: str | None = None,
) -> HTMLResponse:
    """Персональная лента: сюжеты по темам пользователя, фильтр по одной теме."""
    if cursor is None:
        ReadingService.register_visit(user)
        await db.commit()
    topics = await TopicRepository(db).list_for_user(user.id, active_only=True)
    if topic is not None and topic not in {t.id for t in topics}:
        topic = None  # чужая, удалённая или на паузе — показываем все темы
    filters = FeedFilters(personal=True, topic=topic, unread=unread)
    context = await _feed_context(db, request.app.state.settings, user, filters, cursor)
    return templates.TemplateResponse(request, "my_feed.html", {"user": user, "topics": topics, **context})


@router.get("/my/items", response_class=HTMLResponse)
async def my_feed_items(
    request: Request, db: DbSession, user: CurrentUser,
    topic: int | None = None, unread: bool = False, cursor: str | None = None,
) -> HTMLResponse:
    filters = FeedFilters(personal=True, topic=topic, unread=unread)
    context = await _feed_context(db, request.app.state.settings, user, filters, cursor)
    return templates.TemplateResponse(request, "_feed_items.html", {"user": user, **context})


@router.post("/feed/mark-all-read")
async def mark_all_read(db: DbSession, user: CurrentUser, next: Annotated[str, Form()] = "/") -> Response:
    ReadingService.mark_all_read(user)
    await db.commit()
    target = safe_next(next)
    response = RedirectResponse(target, status.HTTP_303_SEE_OTHER)
    response.headers["HX-Redirect"] = target
    return response


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
    topics = TopicRepository(db)
    # Темы, где сюжет есть или где пользователь его отметил (👎 — чтобы было видно и можно было снять)
    reasons = [
        r for r in (await topics.reasons(user.id, [story_id], include_unmatched=True)).get(story_id, [])
        if r.matched or r.vote
    ]
    shown = {r.topic_id for r in reasons}
    other_topics = [t for t in await topics.list_for_user(user.id, active_only=True) if t.id not in shown]
    await ReadingService(db).mark_read(user, story_id)
    await db.commit()
    return templates.TemplateResponse(request, "story.html", {
        "user": user, "story": story, "timeline": timeline, "similar": similar,
        "reasons": reasons, "other_topics": other_topics, "next": f"/stories/{story_id}",
        "now": datetime.now(UTC),
    })
