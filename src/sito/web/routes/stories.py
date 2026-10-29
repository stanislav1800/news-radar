"""Общая лента сюжетов и страница сюжета."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sito.config import Settings
from sito.models import User
from sito.repositories.stories import FeedStory, StoryRepository
from sito.services.reading import ReadingService
from sito.web.deps import CurrentUser, DbSession
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


async def _feed_context(
    db: DbSession, config: Settings, user: User, *, min_sources: int, unread: bool, cursor: str | None,
) -> dict[str, object]:
    min_sources = min_sources if min_sources in SOURCE_FILTERS else 1
    before = _parse_cursor(cursor)
    # На один больше, чем показываем: так понятно, есть ли что-то дальше
    stories = await StoryRepository(db).feed(
        config.feed_page_size + 1,
        user_id=user.id, read_all_before=user.read_all_before,
        min_sources=min_sources, before=before, unread_only=unread,
    )
    has_more = len(stories) > config.feed_page_size
    stories = stories[: config.feed_page_size]
    return {
        "stories": stories,
        "min_sources": min_sources,
        "unread": unread,
        "next_cursor": _cursor(stories[-1]) if has_more else None,
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
    context = await _feed_context(
        db, request.app.state.settings, user, min_sources=min_sources, unread=unread, cursor=cursor
    )
    return templates.TemplateResponse(
        request, "feed.html", {"user": user, "filters": SOURCE_FILTERS, **context}
    )


@router.get("/feed/items", response_class=HTMLResponse)
async def feed_items(
    request: Request, db: DbSession, user: CurrentUser,
    min_sources: int = 1, unread: bool = False, cursor: str | None = None,
) -> HTMLResponse:
    """Следующая порция карточек для HTMX: дописывается в конец ленты."""
    context = await _feed_context(
        db, request.app.state.settings, user, min_sources=min_sources, unread=unread, cursor=cursor
    )
    return templates.TemplateResponse(request, "_feed_items.html", {"user": user, **context})


@router.post("/feed/mark-all-read")
async def mark_all_read(db: DbSession, user: CurrentUser) -> Response:
    ReadingService.mark_all_read(user)
    await db.commit()
    response = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    response.headers["HX-Redirect"] = "/"
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
    await ReadingService(db).mark_read(user, story_id)
    await db.commit()
    return templates.TemplateResponse(
        request, "story.html",
        {"user": user, "story": story, "timeline": timeline, "similar": similar, "now": datetime.now(UTC)},
    )
