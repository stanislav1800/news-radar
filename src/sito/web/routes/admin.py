"""Страницы администратора: подключения и источники."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Annotated

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from sito.models import Connector, ConnectorStatus, User
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.pipeline import PipelineRepository
from sito.repositories.sources import SourceRepository
from sito.services.alerts import find_problems
from sito.services.collector.registry import COLLECTORS, CollectorSpec, RssSourceForm
from sito.services.collector.telegram import TelegramSourceForm
from sito.services.connectors import ConnectorError, ConnectorService, secret_fields
from sito.services.llm import usage_since
from sito.services.sources import SourceError, SourceService
from sito.services.telegram_login import LoginError, QrAttempt, TelegramLoginService
from sito.web.deps import AdminUser, DbSession, require_admin
from sito.web.forms import FormField, form_fields, validation_errors
from sito.web.routes.health import worker_state
from sito.web.security import flash, safe_next
from sito.web.templating import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


FAILED_SHOWN = 20


@router.get("", response_class=HTMLResponse)
async def summary_page(request: Request, db: DbSession, user: AdminUser) -> HTMLResponse:
    """Сводка: что происходит с конвейером и источниками и жив ли worker."""
    now = datetime.now(UTC)
    pipeline = PipelineRepository(db)
    sources = await SourceRepository(db).list_with_stats()
    beat = await pipeline.heartbeat("worker")
    return templates.TemplateResponse(request, "admin/summary.html", {
        "user": user,
        "summary": await pipeline.summary(),
        "lag": await pipeline.lag(),
        "failed": await pipeline.failed_posts(FAILED_SHOWN),
        "broken": [s for s in sources if s.failing],
        "broken_connectors": await _broken_connectors(db),
        "silent": [(s, since) for s in sources if (since := s.silent_since(now))],
        "worker": worker_state(beat.beat_at if beat else None, now),
        "beat": beat,
        "llm": request.app.state.llm,
        "llm_day": await usage_since(db, now - timedelta(days=1)),
        "llm_month": await usage_since(db, now - timedelta(days=30)),
        "llm_limit": request.app.state.settings.llm_daily_tokens,
        "now": now,
    })


async def _broken_connectors(db: DbSession) -> list[Connector]:
    """Включённые подключения, которые не работают: нужен вход (Telegram) или ошибка."""
    return [
        c for c in await ConnectorRepository(db).list_all()
        if c.enabled and not c.builtin and c.status != ConnectorStatus.ACTIVE
    ]


@router.get("/alert", response_class=HTMLResponse)
async def alert(db: DbSession) -> HTMLResponse:
    """Красная точка у меню аккаунта, если что-то сломалось. Подгружается в шапку каждой страницы."""
    now = datetime.now(UTC)
    beat = await PipelineRepository(db).heartbeat("worker")
    problems = [p.text for p in await find_problems(db, now)]
    if worker_state(beat.beat_at if beat else None, now) != "ok":
        problems.insert(0, "worker не работает")
    if not problems:
        return HTMLResponse("")
    return HTMLResponse(f'<span class="alert-dot" title="{escape("; ".join(problems))}"></span>')


@router.post("/failed/retry")
async def retry_failed(request: Request, db: DbSession, post_id: Annotated[int | None, Form()] = None) -> Response:
    count = await PipelineRepository(db).retry_failed([post_id] if post_id is not None else None)
    await db.commit()
    flash(request, f"Вернул в обработку постов: {count}. Worker возьмёт их на ближайшем круге.")
    return RedirectResponse("/admin", status.HTTP_303_SEE_OTHER)


SECRET_KEPT_HELP = "Сохранено. Оставьте пустым, чтобы не менять."


def _form_values(request_form: Mapping[str, object]) -> dict[str, str]:
    return {k: v for k, v in request_form.items() if isinstance(v, str)}


def _connector_fields(
    spec: CollectorSpec, connector: Connector | None,
    values: Mapping[str, str] | None, errors: Mapping[str, str] | None,
) -> list[FormField]:
    """Поля формы подключения. При правке секреты не показываются: пустое поле — оставить прежнее."""
    assert spec.connector_form is not None
    if connector is not None and values is None:
        values = {"name": connector.name, **{k: str(v) for k, v in connector.settings.items() if v is not None}}
    fields = form_fields(spec.connector_form, values, errors)
    if connector is not None and connector.secrets is not None:
        secrets = secret_fields(spec.connector_form)
        fields = [
            replace(f, required=False, help=SECRET_KEPT_HELP) if f.name in secrets and not f.error else f
            for f in fields
        ]
    return fields


def connector_url(connector: Connector) -> str:
    """Страница подключения: у Telegram — каналы и все настройки, у остальных — источники."""
    spec = COLLECTORS.get(connector.type)
    return f"/admin/connectors/{connector.id}" + ("/channels" if spec and spec.login else "")


async def _render_connectors(
    request: Request, db: DbSession, user: User, *,
    target: str | None = None, values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None, status_code: int = status.HTTP_200_OK, detail: int | None = None,
) -> HTMLResponse:
    """Без detail — сводка по всем подключениям; detail — одно подключение целиком (таблица источников).

    target — какая форма открыта с ошибками: source:<id>, edit:<id> или new:<тип>.
    """
    sources = SourceRepository(db)
    sections = []
    now = datetime.now(UTC)
    for connector in await ConnectorRepository(db).list_all():
        if detail is not None and connector.id != detail:
            continue
        spec = COLLECTORS.get(connector.type)
        rows = await sources.list_with_stats(connector.id)
        source_target = target == f"source:{connector.id}"
        edit_target = target == f"edit:{connector.id}"
        sections.append({
            "connector": connector,
            "spec": spec,
            "sources": rows,
            "url": connector_url(connector),
            "summary": {
                "total": len(rows), "collecting": sum(r.enabled for r in rows), "failing": sum(r.failing for r in rows),
                "late": sum(r.late for r in rows), "silent": sum(r.silent_since(now) is not None for r in rows),
            },
            "fields": form_fields(spec.source_form, values if source_target else None,
                                  errors if source_target else None) if spec and spec.source_form else [],
            "form_error": (errors or {}).get("__all__") if source_target else None,
            "form_open": source_target,
            "edit_fields": _connector_fields(spec, connector, values if edit_target else None,
                                             errors if edit_target else None)
                           if spec and spec.connector_form and not connector.builtin else [],
            "edit_error": (errors or {}).get("__all__") if edit_target else None,
            "edit_open": edit_target,
        })
    new_connectors = [
        {
            "spec": spec,
            "fields": _connector_fields(spec, None, values if target == f"new:{spec.source_type}" else None,
                                        errors if target == f"new:{spec.source_type}" else None),
            "form_error": (errors or {}).get("__all__") if target == f"new:{spec.source_type}" else None,
            "form_open": target == f"new:{spec.source_type}",
        }
        for spec in COLLECTORS.values() if spec.connector_form is not None
    ]
    return templates.TemplateResponse(
        request, "admin/connectors.html",
        {"user": user, "sections": sections, "new_connectors": new_connectors, "now": now, "detail": detail},
        status_code=status_code,
    )


def _connector_service(request: Request, db: DbSession) -> ConnectorService:
    return ConnectorService(db, request.app.state.secret_box)


async def _connector(db: DbSession, connector_id: int) -> Connector:
    connector = await ConnectorRepository(db).get(connector_id)
    if connector is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Подключение не найдено")
    return connector


def _back(anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/admin/connectors{anchor}", status.HTTP_303_SEE_OTHER)


@router.get("/connectors", response_class=HTMLResponse)
async def connectors_page(request: Request, db: DbSession, user: AdminUser) -> HTMLResponse:
    return await _render_connectors(request, db, user)


@router.get("/connectors/{connector_id}", response_class=HTMLResponse)
async def connector_page(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> Response:
    connector = await _connector(db, connector_id)
    if connector_url(connector) != f"/admin/connectors/{connector_id}":
        return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)
    return await _render_connectors(request, db, user, detail=connector_id)


@router.post("/connectors")
async def add_connector(request: Request, db: DbSession, user: AdminUser, type: str) -> Response:
    spec = next((s for s in COLLECTORS.values() if s.source_type == type), None)
    if spec is None or spec.connector_form is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Нет такого типа подключений")
    values = _form_values(await request.form())
    target = f"new:{spec.source_type}"
    try:
        form = spec.connector_form.model_validate(values)
        connector = await _connector_service(request, db).create(spec, form)
    except ValidationError as exc:
        return await _render_connectors(request, db, user, target=target, values=values,
                                        errors=validation_errors(exc), status_code=status.HTTP_400_BAD_REQUEST)
    except ConnectorError as exc:
        return await _render_connectors(request, db, user, target=target, values=values,
                                        errors={"__all__": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    await db.commit()
    if spec.login:
        return RedirectResponse(f"/admin/connectors/{connector.id}/login", status.HTTP_303_SEE_OTHER)
    flash(request, f"Подключение «{connector.name}» добавлено. Worker подключится в течение минуты.")
    return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)


@router.post("/connectors/{connector_id}")
async def update_connector(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> Response:
    connector = await _connector(db, connector_id)
    spec = COLLECTORS.get(connector.type)
    if spec is None or spec.connector_form is None or connector.builtin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "У этого подключения нет настроек")
    values = _form_values(await request.form())
    service = _connector_service(request, db)
    target = f"edit:{connector_id}"
    try:
        # Пустое поле секрета — оставить сохранённое значение
        kept = service.current_secrets(connector)
        data = {**values, **{k: str(v) for k, v in kept.items() if not values.get(k)}}
        form = spec.connector_form.model_validate(data)
        await service.update(connector, form, login=spec.login)
    except (ValidationError, ConnectorError) as exc:
        errors = validation_errors(exc) if isinstance(exc, ValidationError) else {"__all__": str(exc)}
        if spec.login:  # настройки Telegram — на странице каналов: там же и сообщение об ошибке
            flash(request, "Не сохранено: " + "; ".join(errors.values()))
            return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)
        return await _render_connectors(request, db, user, target=target, values=values, errors=errors,
                                        status_code=status.HTTP_400_BAD_REQUEST, detail=connector_id)
    await db.commit()
    flash(request, f"Подключение «{connector.name}» сохранено. Worker переподключится в течение минуты.")
    return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)


@router.post("/connectors/{connector_id}/enabled")
async def set_connector_enabled(
    request: Request, db: DbSession, user: AdminUser, connector_id: int, enabled: Annotated[bool, Form()],
) -> Response:
    connector = await _connector(db, connector_id)
    try:
        await _connector_service(request, db).set_enabled(connector, enabled)
    except ConnectorError as exc:
        flash(request, str(exc))
        return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)
    await db.commit()
    flash(request, f"Подключение «{connector.name}» {'включено' if enabled else 'выключено: сбор с него остановится'}.")
    return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)


@router.post("/connectors/{connector_id}/delete")
async def delete_connector(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> Response:
    connector = await _connector(db, connector_id)
    name = connector.name
    try:
        await _connector_service(request, db).delete(connector)
    except ConnectorError as exc:
        flash(request, str(exc))
        return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)
    await db.commit()
    flash(request, f"Подключение «{name}» удалено.")
    return _back()


@router.post("/connectors/{connector_id}/sources")
async def add_source(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> Response:
    connector = await ConnectorRepository(db).get(connector_id)
    spec = COLLECTORS.get(connector.type) if connector else None
    if connector is None or spec is None or spec.source_form is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Подключение не найдено")
    if spec.source_form is TelegramSourceForm:
        return await _add_telegram_channel(request, db, user, connector)

    values = _form_values(await request.form())
    check = values.pop("check", None) == "on"
    try:
        form = RssSourceForm.model_validate(values)
    except ValidationError as exc:
        return await _render_connectors(
            request, db, user, target=f"source:{connector_id}", values=values,
            errors=validation_errors(exc), status_code=status.HTTP_400_BAD_REQUEST, detail=connector_id,
        )

    service = SourceService(db, request.app.state.trial_fetch)
    try:
        source, trial = await service.add_rss(connector, form, check=check)
    except SourceError as exc:
        # Отката нет намеренно: ошибка (дубль, пробный сбор) случается до записи в базу, а rollback
        # «состарил» бы загруженного пользователя, и шаблон упал бы на неявной загрузке (MissingGreenlet)
        hint = " Сохранить без пробного сбора: снимите галочку «Проверить ленту»." if check else ""
        return await _render_connectors(
            request, db, user, target=f"source:{connector_id}", values=values,
            errors={"__all__": f"{exc}{hint}"}, status_code=status.HTTP_400_BAD_REQUEST, detail=connector_id,
        )
    await db.commit()

    message = f"Лента «{source.name}» добавлена."
    if trial:
        message += f" Постов в ленте: {trial.posts}, например: «{trial.titles[0]}»."
    flash(request, message)
    return RedirectResponse(f"/admin/connectors/{connector_id}#source-{source.id}", status.HTTP_303_SEE_OTHER)


async def _add_telegram_channel(request: Request, db: DbSession, user: User, connector: Connector) -> Response:
    """Канал ссылкой без предпросмотра (форма из описания коллектора). Итог — на странице каналов."""
    values = _form_values(await request.form())
    try:
        form = TelegramSourceForm.model_validate(values)
        source = await SourceService(db, request.app.state.trial_fetch).add_telegram(connector, form)
    except ValidationError as exc:
        flash(request, "; ".join(validation_errors(exc).values()))
    except SourceError as exc:
        flash(request, str(exc))
    else:
        await db.commit()
        flash(request, f"Канал {source.name} добавлен. Аккаунт подпишется на него в течение пары минут "
                       "и заберёт посты за последние сутки.")
    return RedirectResponse(f"/admin/connectors/{connector.id}/channels", status.HTTP_303_SEE_OTHER)


# --- Вход в Telegram ------------------------------------------------------------------------------

def _login_service(request: Request, db: DbSession) -> TelegramLoginService:
    return TelegramLoginService(db, request.app.state.secret_box, request.app.state.telegram_login)


async def _login_connector(db: DbSession, connector_id: int) -> Connector:
    connector = await _connector(db, connector_id)
    spec = COLLECTORS.get(connector.type)
    if spec is None or not spec.login:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "У этого подключения нет входа")
    return connector


async def _render_login(
    request: Request, db: DbSession, user: User, connector: Connector, *,
    error: str | None = None, status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    service = _login_service(request, db)
    delivery: tuple[str | None, str | None] = (None, None)
    try:
        step = service.step(connector)
        delivery = service.delivery(connector)
    except ConnectorError as exc:
        step, error = "start", str(exc)
    qr = request.app.state.qr_logins.get(connector.id)
    return templates.TemplateResponse(request, "admin/telegram_login.html", {
        "user": user, "connector": connector, "step": step, "error": error,
        "qr_running": qr is not None and qr.status in ("waiting", "error"),
        "delivery": delivery[0], "next_delivery": delivery[1],
    }, status_code=status_code)


@router.get("/connectors/{connector_id}/login", response_class=HTMLResponse)
async def login_page(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> HTMLResponse:
    return await _render_login(request, db, user, await _login_connector(db, connector_id))


@router.get("/connectors/{connector_id}/login/qr", response_class=HTMLResponse)
async def login_qr(request: Request, db: DbSession, user: AdminUser, connector_id: int) -> Response:
    """Состояние входа по QR-коду для страницы входа (HTMX спрашивает раз в пару секунд)."""
    connector = await _login_connector(db, connector_id)
    attempt: QrAttempt | None = request.app.state.qr_logins.get(connector_id)
    if attempt is not None and attempt.status in ("done", "password"):
        request.app.state.qr_logins.cancel(connector_id)
        if attempt.status == "done":
            flash(request, f"Вход в «{connector.name}» выполнен. Worker подключится в течение минуты "
                           "и заберёт посты каналов за последние сутки.")
            target = f"/admin/connectors/{connector_id}/channels"
        else:
            target = f"/admin/connectors/{connector_id}/login"  # дальше — облачный пароль
        return Response(headers={"HX-Redirect": target})
    qr_svg = None
    if attempt is not None and attempt.status == "waiting" and attempt.url:
        qr_svg = segno.make(attempt.url, error="m").svg_inline(scale=5, border=2)
    return templates.TemplateResponse(request, "admin/_telegram_qr.html", {
        "connector": connector, "attempt": attempt, "qr_svg": qr_svg,
    })


@router.post("/connectors/{connector_id}/login/{action}")
async def login_step(
    request: Request, db: DbSession, user: AdminUser, connector_id: int, action: str,
    code: Annotated[str, Form()] = "", password: Annotated[str, Form()] = "",
) -> Response:
    """Шаги входа: send-code — прислать код, code — ввести код, password — облачный пароль, cancel."""
    connector = await _login_connector(db, connector_id)
    service = _login_service(request, db)
    try:
        if action == "send-code":
            await service.send_code(connector)
        elif action == "code":
            done = await service.enter_code(connector, code)
        elif action == "password":
            done = await service.enter_password(connector, password)
        elif action == "qr":
            request.app.state.qr_logins.start(connector.id, *service.app_credentials(connector))
        elif action == "cancel":
            request.app.state.qr_logins.cancel(connector.id)
            service.cancel(connector)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Нет такого шага")
    except (LoginError, ConnectorError) as exc:
        return await _render_login(request, db, user, connector, error=str(exc),
                                   status_code=status.HTTP_400_BAD_REQUEST)
    await db.commit()
    if action in ("code", "password") and done:
        flash(request, f"Вход в «{connector.name}» выполнен. Worker подключится в течение минуты "
                       "и заберёт посты каналов за последние сутки.")
        return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)
    if action == "cancel":
        return RedirectResponse(connector_url(connector), status.HTTP_303_SEE_OTHER)
    if action == "qr":
        return RedirectResponse(f"/admin/connectors/{connector_id}/login?qr=1", status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/admin/connectors/{connector_id}/login", status.HTTP_303_SEE_OTHER)


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
    source = await repo.get(source_id)
    back = f"/admin/connectors/{source.connector_id}" if source else "/admin/connectors"
    return RedirectResponse(f"{back}#source-{source_id}", status.HTTP_303_SEE_OTHER)


@router.post("/sources/{source_id}/rename")
async def rename_source(
    request: Request, db: DbSession, source_id: int, name: Annotated[str, Form()], next: Annotated[str, Form()] = "",
) -> Response:
    """Своё название источника в ленте: «TACC» латиницей, как назвал себя канал, — в «ТАСС»."""
    name = " ".join(name.split())[:200]
    if not name:
        flash(request, "Название не может быть пустым")
    elif not await SourceRepository(db).rename(source_id, name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Источник не найден")
    else:
        await db.commit()
        flash(request, f"Переименован в «{name}».")
    return RedirectResponse(safe_next(next or "/admin/connectors"), status.HTTP_303_SEE_OTHER)


@router.post("/sources/{source_id}/delete")
async def delete_source(request: Request, db: DbSession, source_id: int) -> Response:
    repo = SourceRepository(db)
    source = await repo.get(source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Источник не найден")
    name = source.name
    await repo.delete(source_id)
    await db.commit()
    flash(request, f"Источник «{name}» удалён вместе с его постами.")
    back = f"/admin/connectors/{source.connector_id}"
    response = RedirectResponse(back, status.HTTP_303_SEE_OTHER)
    response.headers["HX-Redirect"] = back
    return response
