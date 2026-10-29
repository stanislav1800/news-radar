"""Страницы администратора: подключения и источники."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from sito.models import User
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.sources import SourceRepository
from sito.services.collector.registry import COLLECTORS, RssSourceForm
from sito.services.sources import SourceError, SourceService
from sito.web.deps import AdminUser, DbSession, require_admin
from sito.web.forms import form_fields, validation_errors
from sito.web.security import flash
from sito.web.templating import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("")
async def admin_home() -> Response:
    return RedirectResponse("/admin/connectors", status.HTTP_303_SEE_OTHER)


async def _render_connectors(
    request: Request, db: DbSession, user: User, *,
    form_connector_id: int | None = None, values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None, status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    sources = SourceRepository(db)
    sections = []
    for connector in await ConnectorRepository(db).list_all():
        spec = COLLECTORS.get(connector.type)
        is_form_target = connector.id == form_connector_id
        sections.append({
            "connector": connector,
            "spec": spec,
            "sources": await sources.list_with_stats(connector.id),
            "fields": form_fields(spec.source_form, values if is_form_target else None,
                                  errors if is_form_target else None) if spec else [],
            "form_error": (errors or {}).get("__all__") if is_form_target else None,
            "form_open": is_form_target,
        })
    return templates.TemplateResponse(
        request, "admin/connectors.html",
        {"user": user, "sections": sections, "now": datetime.now(UTC)}, status_code=status_code
    )


@router.get("/connectors", response_class=HTMLResponse)
async def connectors_page(request: Request, db: DbSession, user: AdminUser) -> HTMLResponse:
    return await _render_connectors(request, db, user)


@router.post("/connectors/{connector_id}/sources")
async def add_source(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> Response:
    connector = await ConnectorRepository(db).get(connector_id)
    if connector is None or connector.type not in COLLECTORS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Подключение не найдено")

    form_data = await request.form()
    values = {k: v for k, v in form_data.items() if isinstance(v, str)}
    check = values.pop("check", None) == "on"
    try:
        form = RssSourceForm.model_validate(values)
    except ValidationError as exc:
        return await _render_connectors(
            request, db, user, form_connector_id=connector_id, values=values,
            errors=validation_errors(exc), status_code=status.HTTP_400_BAD_REQUEST,
        )

    service = SourceService(db, request.app.state.trial_fetch)
    try:
        source, trial = await service.add_rss(connector, form, check=check)
    except SourceError as exc:
        # Отката нет намеренно: ошибка (дубль, пробный сбор) случается до записи в базу, а rollback
        # «состарил» бы загруженного пользователя, и шаблон упал бы на неявной загрузке (MissingGreenlet)
        hint = " Сохранить без пробного сбора: снимите галочку «Проверить ленту»." if check else ""
        return await _render_connectors(
            request, db, user, form_connector_id=connector_id, values=values,
            errors={"__all__": f"{exc}{hint}"}, status_code=status.HTTP_400_BAD_REQUEST,
        )
    await db.commit()

    message = f"Лента «{source.name}» добавлена."
    if trial:
        message += f" Постов в ленте: {trial.posts}, например: «{trial.titles[0]}»."
    flash(request, message)
    return RedirectResponse(f"/admin/connectors#source-{source.id}", status.HTTP_303_SEE_OTHER)


@router.post("/sources/{source_id}/enabled")
async def set_source_enabled(
    request: Request, db: DbSession, user: AdminUser, source_id: int, enabled: Annotated[bool, Form()],
) -> Response:
    repo = SourceRepository(db)
    if not await repo.set_enabled(source_id, enabled):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Источник не найден")
    await db.commit()
    if request.headers.get("HX-Request"):
        # HTMX заменяет только строку таблицы
        return templates.TemplateResponse(
            request, "admin/_source_row.html",
            {"user": user, "s": await repo.get_row(source_id), "now": datetime.now(UTC)},
        )
    return RedirectResponse(f"/admin/connectors#source-{source_id}", status.HTTP_303_SEE_OTHER)


@router.post("/sources/{source_id}/delete")
async def delete_source(request: Request, db: DbSession, source_id: int) -> Response:
    repo = SourceRepository(db)
    source = await repo.get(source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Источник не найден")
    name = source.name
    await repo.delete(source_id)
    await db.commit()
    flash(request, f"Лента «{name}» удалена вместе с её постами.")
    response = RedirectResponse("/admin/connectors", status.HTTP_303_SEE_OTHER)
    response.headers["HX-Redirect"] = "/admin/connectors"
    return response
