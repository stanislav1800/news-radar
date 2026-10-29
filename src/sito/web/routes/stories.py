"""Общая лента сюжетов и страница сюжета."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from sito.config import Settings
from sito.repositories.stories import FeedStory, StoryRepository
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


async def _feed_context(db: DbSession, config: Settings, min_sources: int, cursor: str | None) -> dict[str, object]:
    min_sources = min_sources if min_sources in SOURCE_FILTERS else 1
    # На один больше, чем показываем: так понятно, есть ли что-то дальше
    stories = await StoryRepository(db).feed(config.feed_page_size + 1, min_sources, _parse_cursor(cursor))
    has_more = len(stories) > config.feed_page_size
    stories = stories[: config.feed_page_size]
    return {
        "stories": stories,
        "min_sources": min_sources,
        "next_cursor": _cursor(stories[-1]) if has_more else None,
    }


@router.get("/", response_class=HTMLResponse)
async def feed(
    request: Request, db: DbSession, user: CurrentUser, min_sources: int = 1, cursor: str | None = None
) -> HTMLResponse:
    context = await _feed_context(db, request.app.state.settings, min_sources, cursor)
    return templates.TemplateResponse(
        request, "feed.html", {"user": user, "filters": SOURCE_FILTERS, "now": datetime.now(UTC), **context}
    )


@router.get("/feed/items", response_class=HTMLResponse)
async def feed_items(
    request: Request, db: DbSession, user: CurrentUser, min_sources: int = 1, cursor: str | None = None
) -> HTMLResponse:
    """Следующая порция карточек для HTMX: дописывается в конец ленты."""
    context = await _feed_context(db, request.app.state.settings, min_sources, cursor)
    return templates.TemplateResponse(
        request, "_feed_items.html", {"user": user, "now": datetime.now(UTC), **context}
    )


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
    return templates.TemplateResponse(
        request, "story.html",
        {"user": user, "story": story, "timeline": timeline, "similar": similar, "now": datetime.now(UTC)},
    )
