"""Каналы Telegram-подключения: список с «собирать / не собирать», подписка по ссылкам с предпросмотром,
отписка, настройки (что делать с новыми каналами, тишина на телефоне).

В Telegram веб сам не ходит: сессией пользуется только worker. Веб меняет базу (включить, выключить,
канал по ссылке ждёт подписки) или оставляет worker'у поручение (показать канал, отписаться).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sito.models import Connector, Source, SourceType
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.sources import SourceRepository, SourceRow
from sito.services.collector.registry import COLLECTORS
from sito.services.collector.telegram import parse_link
from sito.services.connectors import ConnectorService
from sito.web.deps import AdminUser, DbSession, require_admin
from sito.web.routes.admin import _connector_fields
from sito.web.security import flash
from sito.web.templating import templates

router = APIRouter(prefix="/admin/connectors/{connector_id}", dependencies=[Depends(require_admin)])

MAX_LINKS = 50
CHANNELS_LIMIT = 500  # столько каналов Telegram даёт обычному аккаунту (с Premium — 1000)  # ссылок за раз: подписка всё равно идёт по одной в минуту


@dataclass(frozen=True, slots=True)
class ChannelRow:
    source: SourceRow
    # waiting — ждёт решения, collecting, stopped, pending — ждёт подписки, error — подписаться не вышло,
    # leaving — отписывается, left — отписан
    state: str
    # Собирается, но постов нет дольше обычного для канала: обычно канал заброшен — кандидат на отписку
    silent: bool = False

    @property
    def username(self) -> str | None:
        url = self.source.url or ""
        return url.rsplit("/", 1)[-1] if url.startswith("https://t.me/") and "/+" not in url else None


def _state(row: SourceRow) -> str:
    if row.info.get("left"):
        return "left"
    if row.info.get("leaving"):
        return "leaving"
    if row.external_id is None:
        return "error" if row.last_error else "pending"
    if row.enabled:
        return "collecting"
    return "stopped" if row.info.get("decided") else "waiting"


# Порядок в списке: сначала то, что требует решения
_ORDER = {"waiting": 0, "error": 1, "pending": 2, "collecting": 3, "stopped": 4, "leaving": 5, "left": 6}


async def _telegram(db: DbSession, connector_id: int) -> Connector:
    connector = await ConnectorRepository(db).get(connector_id)
    if connector is None or connector.type != SourceType.TELEGRAM:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Подключение не найдено")
    return connector


def _back(connector_id: int, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/admin/connectors/{connector_id}/channels{anchor}", status.HTTP_303_SEE_OTHER)


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(
    request: Request, db: DbSession, user: AdminUser, connector_id: int, preview: int | None = None,
    show: str = "", tab: str = "list",
) -> HTMLResponse:
    """Вкладки: list — каналы, add — добавить (там же ответ на предпросмотр), settings — настройки.
    show — фильтр списка при открытии (silent — «Молчат», ссылка со страницы «Состояние»)."""
    connector = await _telegram(db, connector_id)
    tab = "add" if preview else tab if tab in ("list", "add", "settings") else "list"
    now = datetime.now(UTC)
    rows = [ChannelRow(r, state, state == "collecting" and r.silent_since(now) is not None)
            for r in await SourceRepository(db).list_with_stats(connector_id) if (state := _state(r))]
    rows.sort(key=lambda r: (_ORDER[r.state], r.source.name.lower()))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    counts["silent"] = sum(row.silent for row in rows)
    return templates.TemplateResponse(request, "admin/telegram_channels.html", {
        "user": user, "connector": connector, "rows": rows, "counts": counts, "preview_id": preview, "show": show, "tab": tab,
        "subscribed": sum(r.state not in ("left", "pending", "error") for r in rows), "limit": CHANNELS_LIMIT,
        "new_channels": connector.settings.get("new_channels", "collect"),
        "quiet": bool(connector.settings.get("quiet", True)), "alerts": bool(connector.settings.get("alerts", True)),
        "now": now,
        # Подключение целиком здесь же: вход, учётные данные, включить, удалить
        "fields": _connector_fields(COLLECTORS[SourceType.TELEGRAM], connector, None, None),
    })


@router.post("/channels/options")
async def save_options(
    request: Request, db: DbSession, connector_id: int,
    new_channels: Annotated[str, Form()] = "collect", quiet: Annotated[bool, Form()] = False,
    alerts: Annotated[bool, Form()] = False,
) -> Response:
    connector = await _telegram(db, connector_id)
    await ConnectorService(db, request.app.state.secret_box).set_options(
        connector, new_channels="wait" if new_channels == "wait" else "collect", quiet=quiet, alerts=alerts,
    )
    await db.commit()
    flash(request, "Настройки сохранены. Worker переподключится в течение минуты.")
    return _back(connector_id, "?tab=settings")


# --- Добавление: ссылки → предпросмотр → подписка ---------------------------------------------------


@router.post("/channels/preview")
async def preview_links(request: Request, db: DbSession, connector_id: int, links: Annotated[str, Form()]) -> Response:
    await _telegram(db, connector_id)
    if not links.strip():
        flash(request, "Вставьте ссылки на каналы")
        return _back(connector_id, "?tab=add")
    return await _command(request, db, connector_id, "preview", {"links": links[:10_000]})


@router.post("/channels/search")
async def search_channels(request: Request, db: DbSession, connector_id: int, query: Annotated[str, Form()]) -> Response:
    """Поиск каналов Telegram по словам: результат — как предпросмотр, с подпиской в один клик."""
    return await _command(request, db, connector_id, "search", {"query": query.strip()[:100]})


@router.post("/channels/similar")
async def similar_channels(request: Request, db: DbSession, connector_id: int, source_id: Annotated[int, Form()]) -> Response:
    return await _command(request, db, connector_id, "similar", {"source_id": source_id})


@router.post("/logout")
async def logout(request: Request, db: DbSession, connector_id: int) -> Response:
    await _command(request, db, connector_id, "logout", {})
    flash(request, "Выхожу из аккаунта: сессия завершится и в Telegram. Чтобы снова собирать каналы, войдите заново.")
    return _back(connector_id, "?tab=settings")


async def _command(request: Request, db: DbSession, connector_id: int, kind: str, payload: dict[str, Any]) -> Response:
    await _telegram(db, connector_id)
    command = await ConnectorRepository(db).add_command(connector_id, kind, payload)
    await db.commit()
    return _back(connector_id, f"?preview={command.id}#add")


@router.get("/commands/{command_id}", response_class=HTMLResponse)
async def command_state(
    request: Request, db: DbSession, user: AdminUser, connector_id: int, command_id: int,
) -> HTMLResponse:
    """Предпросмотр каналов: пока worker не ответил, страница спрашивает раз в секунду."""
    connector = await _telegram(db, connector_id)
    command = await ConnectorRepository(db).get_command(command_id)
    if command is None or command.connector_id != connector_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поручение не найдено")
    waited = (datetime.now(UTC) - command.created_at).total_seconds()
    return templates.TemplateResponse(request, "admin/_telegram_preview.html", {
        "user": user, "connector": connector, "command": command, "items": command.result or [],
        "slow": waited > 20,
    })


@router.post("/channels/subscribe")
async def subscribe(
    request: Request, db: DbSession, connector_id: int, link: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Отмеченные каналы ждут подписки: worker подпишется по одному в минуту."""
    connector = await _telegram(db, connector_id)
    repo = SourceRepository(db)
    added = 0
    for raw in (link or [])[:MAX_LINKS]:
        try:
            url = parse_link(raw).url
        except ValueError:
            continue
        if await repo.get_by_url(url):
            continue
        await repo.add(connector.id, connector.type, _name(url), url=url)
        added += 1
    await db.commit()
    if added:
        flash(request, f"Аккаунт подпишется на {added} — по одному в минуту, и заберёт посты за последние сутки.")
    else:
        flash(request, "Новых каналов не отмечено: эти уже есть в списке.")
    return _back(connector_id)


def _name(url: str) -> str:
    """Имя до подписки: потом worker поставит название канала."""
    tail = url.rsplit("/", 1)[-1]
    return "приглашение " + tail if tail.startswith("+") else "@" + tail


# --- Действия со списком ------------------------------------------------------------------------------


@router.post("/channels/action")
async def channels_action(
    request: Request, db: DbSession, connector_id: int, action: Annotated[str, Form()],
    source_id: Annotated[list[int] | None, Form()] = None,
) -> Response:
    """collect — собирать, stop — не собирать, leave — отписаться (посты остаются),
    delete — отписаться и удалить посты, rejoin — подписаться снова, forget — убрать ссылку с ошибкой."""
    await _telegram(db, connector_id)
    ids = source_id or []
    if not ids:
        flash(request, "Отметьте каналы")
        return _back(connector_id)
    sources = [s for s in await SourceRepository(db).list_for_connector(connector_id) if s.id in set(ids)]
    message = _apply(action, sources)
    commands = ConnectorRepository(db)
    for source in sources:
        if action not in ("leave", "delete", "forget"):
            continue
        subscribed = source.external_id is not None and not source.info.get("left")
        if subscribed and action != "forget":
            await commands.add_command(connector_id, "leave", {"source_id": source.id, "delete": action == "delete"})
        elif source.external_id is None or action != "leave":
            # Подписки нет (ссылка ещё ждёт или с ошибкой, канал уже отписан) — просто убрать из списка
            await SourceRepository(db).delete(source.id)
    await db.commit()
    flash(request, message.format(n=len(sources)))
    return _back(connector_id)


def _apply(action: str, sources: list[Source]) -> str:
    """Изменения в базе сразу (worker увидит их на ближайшем круге); текст — для сообщения."""
    if action in ("collect", "stop"):
        for source in sources:
            source.enabled = action == "collect"
            source.info = {**source.info, "decided": True}
        return "Собираются: {n}." if action == "collect" else "Больше не собираются: {n}. Посты остаются, подписка тоже."
    if action in ("leave", "delete"):
        for source in sources:
            source.enabled = False
            if source.external_id is not None and not source.info.get("left"):
                source.info = {**source.info, "leaving": True}
        return ("Отписываюсь от каналов: {n}. Уже собранные посты остаются." if action == "leave"
                else "Удаляю каналы вместе с их постами: {n}. Через пару секунд они пропадут из списка.")
    if action == "rejoin":
        for source in sources:
            if source.url and source.info.get("left"):
                # Снова ждёт подписки: worker подпишется по ссылке и вернёт id канала
                source.external_id = None
                source.enabled = True
                source.last_error = None
                source.info = {k: v for k, v in source.info.items() if k not in ("left", "quiet")}
        return "Аккаунт подпишется снова: {n}. У каналов без открытой ссылки подписку не вернуть — нужно приглашение."
    if action == "forget":
        return "Убрано из списка: {n}."
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нет такого действия")

