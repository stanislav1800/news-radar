"""Поиск, темы, 👍/👎 и RSS-лента темы."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from html import escape
from typing import Annotated
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from sito.config import Settings
from sito.models import Topic, User
from sito.repositories.stories import StoryRepository
from sito.repositories.topics import TopicReason, TopicRepository
from sito.services.search import DEFAULT_PERIOD, SearchService
from sito.services.topics import (
    TopicError,
    TopicForm,
    TopicRules,
    TopicService,
    Vote,
    topic_name_from,
)
from sito.utils.lemmas import parse_query
from sito.web.deps import CurrentUser, DbSession
from sito.web.forms import form_fields, validation_errors
from sito.web.security import flash, safe_next
from sito.web.templating import templates

router = APIRouter()

RSS_ITEMS = 50


def _service(request: Request, db: DbSession) -> TopicService:
    return TopicService(db, TopicRules.from_settings(request.app.state.settings))


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


async def _owned(db: DbSession, user: User, topic_id: int) -> Topic:
    topic = await TopicRepository(db).get_owned(topic_id, user.id)
    if topic is None:  # чужая тема — как несуществующая: не выдаём, что она есть
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Тема не найдена")
    return topic


def _matched_text(added: int, removed: int) -> str:
    parts = []
    if added:
        parts.append(f"добавлено сюжетов: {added}")
    if removed:
        parts.append(f"убрано: {removed}")
    return ", ".join(parts)


# --- Поиск ---------------------------------------------------------------------------------------

@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, db: DbSession, user: CurrentUser, q: str = "", period: int = DEFAULT_PERIOD,
) -> HTMLResponse:
    config: Settings = request.app.state.settings
    results = await SearchService(db, request.app.state.embedder, config.search_limit).search(user, q, period)
    # Что найдёт тема по этим словам, если нажать «Следить»: поиск показывает и частичные совпадения,
    # а тема берёт только сюжеты со всеми словами
    preview = None
    if results.query:
        since = datetime.now(UTC) - timedelta(days=config.topic_backfill_days)
        preview = TopicPreview(
            found=await TopicRepository(db).preview(results.query, since),
            days=config.topic_backfill_days,
            single_word=parse_query(results.query).single_word is not None,
            wide_after=config.topic_wide_per_day * config.topic_backfill_days,
        )
    return templates.TemplateResponse(request, "search.html", {
        "user": user, "results": results, "search_query": results.query, "preview": preview,
        "topic_name": topic_name_from(results.query) if results.query else "",
        "now": datetime.now(UTC),
    })


@dataclass(frozen=True, slots=True)
class TopicPreview:
    found: int         # сколько сюжетов тема нашла бы за days дней
    days: int
    single_word: bool  # запрос из одного слова: у слова бывает несколько значений
    wide_after: float  # с какого числа найденного тема считается широкой

    @property
    def wide(self) -> bool:
        return self.found >= self.wide_after


# --- Список тем и создание -----------------------------------------------------------------------

@router.get("/topics")
async def topics_page() -> Response:
    """Темы — вкладки ленты; отдельного списка больше нет."""
    return _redirect("/")


@router.post("/topics")
async def create_topic(
    request: Request, db: DbSession, user: CurrentUser,
    name: Annotated[str, Form()] = "", query: Annotated[str, Form()] = "",
    like: Annotated[list[int] | None, Form()] = None, dislike: Annotated[list[int] | None, Form()] = None,
) -> Response:
    """«Следить» из поиска: запрос становится темой, отмеченные в результатах 👍/👎 — её первыми примерами."""
    try:
        form = TopicForm(name=name or topic_name_from(query), query=query)
    except ValidationError:
        flash(request, "Чтобы следить, введите, что искать")
        return _redirect("/search")
    topic = await _service(request, db).create(user, form.name, form.query, like or [], dislike or [])
    await db.commit()
    count = await TopicRepository(db).matched_count(topic.id)
    examples = len(like or []) + len(dislike or [])
    flash(request, f"Тема «{topic.name}» создана. Сейчас в ней сюжетов: {count}"
                   + (f", примеров: {examples}." if examples else ".")
                   + " Лишнее убирайте кнопкой «Не по теме» в меню «⋯» у сюжета.")
    return _redirect(f"/?topic={topic.id}")


@router.post("/topics/follow")
async def follow_story(request: Request, db: DbSession, user: CurrentUser, story_id: Annotated[int, Form()]) -> Response:
    """«Следить за такими»: сюжет становится первым примером новой темы.

    С HTMX тема создаётся на месте: страница сюжета остаётся открытой, появляется строка с итогом.
    """
    htmx = bool(request.headers.get("HX-Request"))
    try:
        topic = await _service(request, db).follow_story(user, story_id)
    except TopicError as exc:
        if htmx:
            return templates.TemplateResponse(request, "_reason_error.html", {"message": str(exc)})
        flash(request, str(exc))
        return _redirect(f"/stories/{story_id}")
    await db.commit()
    if htmx:
        count = await TopicRepository(db).matched_count(topic.id)
        return templates.TemplateResponse(request, "_followed.html", {"topic": topic, "count": count})
    flash(request, f"Тема «{topic.name}» создана: в неё попадут сюжеты, похожие на этот.")
    return _redirect(f"/?topic={topic.id}")


# --- 👍/👎 ---------------------------------------------------------------------------------------

@router.post("/topics/vote", response_class=HTMLResponse)
async def vote(
    request: Request, db: DbSession, user: CurrentUser,
    topic_id: Annotated[int, Form()], story_id: Annotated[int, Form()], vote: Annotated[Vote, Form()],
    next: Annotated[str, Form()] = "/", mode: Annotated[str, Form()] = "line",
) -> Response:
    """«Не по теме», «Добавить в тему», «Отменить». HTMX получает обновлённую строку с итогом
    (сколько похожих сюжетов добавилось или убралось), без JS — переход назад."""
    try:
        result = await _service(request, db).vote(user, topic_id, story_id, vote)
    except TopicError as exc:
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "_reason_error.html", {"message": str(exc)})
        flash(request, str(exc))
        return _redirect(safe_next(next))
    await db.commit()
    if not request.headers.get("HX-Request"):
        return _redirect(safe_next(next))
    reasons = await TopicRepository(db).reasons(user.id, [story_id], topic_ids=[topic_id], include_unmatched=True)
    [reason] = reasons[story_id]
    # Сам сюжет тоже входит в счёт: «и ещё N похожих» — без него
    others = max(0, (result.added if vote is Vote.LIKE else result.removed) - 1)
    done = None if vote is Vote.NONE else str(vote)
    if mode in ("feed", "suggest"):
        return await _card_response(request, db, user, reason, mode, done, others)
    return templates.TemplateResponse(request, "_reason.html", {
        "r": reason, "next": safe_next(next), "done": done, "others": others,
    })


async def _card_response(
    request: Request, db: DbSession, user: User, reason: TopicReason, mode: str, done: str | None, others: int,
) -> HTMLResponse:
    """Ответ для карточки в ленте: итог действия с «Отменить» или, после отмены, снова сама карточка."""
    if done is not None:
        return templates.TemplateResponse(request, "_card_result.html", {
            "r": reason, "done": done, "others": others, "mode": mode,
        })
    [card] = await StoryRepository(db).cards([reason.story_id], user_id=user.id, read_all_before=user.read_all_before)
    if mode == "suggest":
        context: dict[str, object] = {"suggest": reason}
    else:
        active = [t.id for t in await TopicRepository(db).list_for_user(user.id, active_only=True)]
        story_reasons = await TopicRepository(db).reasons(user.id, [card.id], topic_ids=active)
        context = {"story_reasons": story_reasons.get(card.id, [])}
    return templates.TemplateResponse(request, "_card_single.html", {"user": user, "s": card, "now": datetime.now(UTC), **context})


# --- Страница темы -------------------------------------------------------------------------------

async def _render_topic(
    request: Request, db: DbSession, user: User, topic: Topic, *,
    values: dict[str, str] | None = None, errors: dict[str, str] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    examples = await TopicRepository(db).examples(topic.id)
    form_values = values or {"name": topic.name, "query": topic.query or ""}
    return templates.TemplateResponse(request, "topic.html", {
        "user": user, "topic": topic,
        "likes": [e for e in examples if e.liked], "dislikes": [e for e in examples if not e.liked],
        "words": parse_query(topic.query).words if topic.query else [],
        "fields": form_fields(TopicForm, form_values, errors), "form_open": errors is not None,
        "rss_url": f"{request.base_url}rss/{topic.rss_token}",
        "default_similarity": request.app.state.settings.topic_example_min_similarity,
        "next": f"/topics/{topic.id}",
        "now": datetime.now(UTC),
    }, status_code=status_code)


@router.get("/topics/{topic_id}", response_class=HTMLResponse)
async def topic_page(request: Request, db: DbSession, user: CurrentUser, topic_id: int) -> HTMLResponse:
    return await _render_topic(request, db, user, await _owned(db, user, topic_id))


@router.post("/topics/{topic_id}")
async def update_topic(request: Request, db: DbSession, user: CurrentUser, topic_id: int) -> Response:
    topic = await _owned(db, user, topic_id)
    values = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        form = TopicForm.model_validate(values)
    except ValidationError as exc:
        return await _render_topic(request, db, user, topic, values=values, errors=validation_errors(exc),
                                   status_code=status.HTTP_400_BAD_REQUEST)
    await _service(request, db).update(user, topic_id, form)
    await db.commit()
    flash(request, "Тема сохранена")
    return _redirect(f"/topics/{topic_id}")


@router.post("/topics/{topic_id}/pause")
async def pause_topic(
    request: Request, db: DbSession, user: CurrentUser, topic_id: int, paused: Annotated[bool, Form()],
) -> Response:
    await _owned(db, user, topic_id)
    topic = await _service(request, db).set_paused(user, topic_id, paused)
    await db.commit()
    flash(request, f"Тема «{topic.name}» на паузе: не пополняется и не видна в ленте" if paused
                   else f"Тема «{topic.name}» снова работает")
    return _redirect(f"/topics/{topic_id}")


@router.post("/topics/{topic_id}/delete")
async def delete_topic(request: Request, db: DbSession, user: CurrentUser, topic_id: int) -> Response:
    await _owned(db, user, topic_id)
    name = await _service(request, db).delete(user, topic_id)
    await db.commit()
    if request.headers.get("HX-Request"):  # «Отменить» сразу после «Следить за такими»
        return HTMLResponse(f'<div class="reason out"><span class="why">Тема «{escape(name)}» удалена.</span></div>')
    flash(request, f"Тема «{name}» удалена")
    return _redirect("/")


@router.post("/topics/{topic_id}/rss-token")
async def new_rss_token(request: Request, db: DbSession, user: CurrentUser, topic_id: int) -> Response:
    await _owned(db, user, topic_id)
    await _service(request, db).new_rss_token(user, topic_id)
    await db.commit()
    flash(request, "Адрес RSS-ленты сменён, старый больше не работает")
    return _redirect(f"/topics/{topic_id}")


@router.post("/topics/{topic_id}/examples/{example_id}/delete")
async def delete_example(request: Request, db: DbSession, user: CurrentUser, topic_id: int, example_id: int) -> Response:
    await _owned(db, user, topic_id)
    result = await _service(request, db).remove_example(user, topic_id, example_id)
    await db.commit()
    changes = _matched_text(result.added, result.removed)
    flash(request, "Пример убран" + (f": {changes}" if changes else ""))
    return _redirect(f"/topics/{topic_id}")


# --- RSS -----------------------------------------------------------------------------------------

@router.get("/rss/{token}")
async def topic_rss(request: Request, db: DbSession, token: str) -> Response:
    """RSS-лента темы. Без входа: доступ даёт секрет в адресе, его видит только владелец темы."""
    repo = TopicRepository(db)
    topic = await repo.by_rss_token(token)
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Лента не найдена")
    stories = await StoryRepository(db).cards(
        await repo.matched_story_ids(topic.id, RSS_ITEMS), user_id=topic.user_id
    )
    base = str(request.base_url)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"Сито · {topic.name}"
    ET.SubElement(channel, "link").text = f"{base}?topic={topic.id}"
    ET.SubElement(channel, "description").text = f"Сюжеты по теме «{topic.name}»"
    ET.SubElement(channel, "language").text = "ru"
    for story in stories:
        item = ET.SubElement(channel, "item")
        url = f"{base}stories/{story.id}"
        ET.SubElement(item, "title").text = story.title
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", isPermaLink="true").text = url
        ET.SubElement(item, "pubDate").text = format_datetime(story.started_at)
        sources = f"Изданий: {story.sources_count}" + (f", первым — {story.first_source_name}" if story.first_source_name else "")
        ET.SubElement(item, "description").text = f"{story.lead}\n\n{sources}" if story.lead else sources
    body = ET.tostring(rss, encoding="unicode", xml_declaration=True)
    return Response(body, media_type="application/rss+xml; charset=utf-8")
