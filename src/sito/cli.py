"""Консольные команды Сита.

    uv run sito source add "ТАСС" https://tass.ru/rss/v2.xml
    uv run sito source list
    uv run sito source disable 3
    uv run sito user create admin --admin
    uv run sito web
"""

import asyncio
from collections.abc import Coroutine
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from sito.config import settings
from sito.db.engine import async_session, engine
from sito.log import setup_logging
from sito.models import SourceType
from sito.repositories.connectors import ConnectorRepository
from sito.repositories.sources import SourceRepository
from sito.repositories.stories import StoryRepository
from sito.repositories.users import UserRepository
from sito.services.clustering import ClusteringRules, ClusteringService
from sito.services.collector.registry import RssSourceForm
from sito.services.sources import SourceError, SourceService, http_trial_fetch
from sito.services.users import UserError, UserService

app = typer.Typer(help="Сито: персональный новостной радар.", no_args_is_help=True)
source_app = typer.Typer(help="Источники встроенного RSS-подключения.", no_args_is_help=True)
app.add_typer(source_app, name="source")
user_app = typer.Typer(help="Пользователи веба.", no_args_is_help=True)
app.add_typer(user_app, name="user")
pipeline_app = typer.Typer(help="Обслуживание пайплайна.", no_args_is_help=True)
app.add_typer(pipeline_app, name="pipeline")

console = Console()


@app.callback()
def _setup() -> None:
    setup_logging(settings.log_level, settings.log_format, settings.log_sql)


@source_app.command("add")
def source_add(
    name: Annotated[str, typer.Argument(help="Название для людей, например «ТАСС».")],
    url: Annotated[str, typer.Argument(help="Адрес RSS-ленты.")],
    interval: Annotated[int, typer.Option(min=60, help="Как часто опрашивать, секунд.")] = 300,
    check: Annotated[bool, typer.Option(help="Сделать пробный сбор перед сохранением.")] = True,
) -> None:
    """Добавить RSS-ленту. Перед сохранением делает пробный сбор и показывает результат."""
    try:
        form = RssSourceForm(name=name, url=url, poll_interval=interval)  # type: ignore[arg-type]
    except ValidationError as exc:
        console.print(f"[red]{exc.errors()[0]['msg']}[/red]")
        raise typer.Exit(1) from exc

    async def run() -> None:
        async with async_session() as session, session.begin():
            connector = await ConnectorRepository(session).get_builtin(SourceType.RSS)
            if connector is None:
                console.print("[red]Нет встроенного RSS-подключения: примените миграции (alembic upgrade head)[/red]")
                raise typer.Exit(1)
            service = SourceService(session, http_trial_fetch(settings))
            try:
                with console.status("Пробный сбор..." if check else "Сохранение..."):
                    source, trial = await service.add_rss(connector, form, check=check)
            except SourceError as exc:
                console.print(f"[red]{exc}[/red]")
                if check:
                    console.print("Не сохранено. Добавить без проверки: --no-check")
                raise typer.Exit(1) from exc
        if trial:
            console.print(f"[green]Лента рабочая:[/green] постов {trial.posts}, например:")
            for title in trial.titles:
                console.print(f"  • {title}")
        console.print(f"Сохранено: id={source.id}. Worker опросит его на ближайшем круге.")

    _run(run())


@source_app.command("list")
def source_list() -> None:
    """Показать все источники и их состояние."""

    async def run() -> None:
        async with async_session() as session:
            sources = await SourceRepository(session).list_all()
        if not sources:
            console.print("Источников пока нет. Добавить: sito source add <название> <адрес>")
            return

        table = Table()
        for column in ("id", "тип", "название", "вкл", "интервал", "последний успех", "ошибка"):
            table.add_column(column)
        for s in sources:
            table.add_row(
                str(s.id),
                s.type,
                s.name,
                "да" if s.enabled else "[dim]нет[/dim]",
                f"{s.poll_interval} с" if s.poll_interval else "сразу",
                f"{s.last_success_at.astimezone():%Y-%m-%d %H:%M}" if s.last_success_at else "—",
                f"[red]{s.last_error[:60]}[/red]" if s.last_error else "",
            )
        console.print(table)

    _run(run())


def _set_enabled(source_id: int, enabled: bool) -> None:
    async def run() -> None:
        async with async_session() as session, session.begin():
            found = await SourceRepository(session).set_enabled(source_id, enabled)
        if not found:
            console.print(f"[red]Источника {source_id} нет[/red]")
            raise typer.Exit(1)
        console.print(f"Источник {source_id} {'включён' if enabled else 'выключен'}")

    _run(run())


@source_app.command("enable")
def source_enable(source_id: int) -> None:
    """Включить источник."""
    _set_enabled(source_id, True)


@source_app.command("disable")
def source_disable(source_id: int) -> None:
    """Выключить источник: worker перестанет его опрашивать, посты останутся."""
    _set_enabled(source_id, False)


def _ask_password() -> str:
    return str(typer.prompt("Пароль", hide_input=True, confirmation_prompt="Повторите пароль"))


@user_app.command("create")
def user_create(
    username: Annotated[str, typer.Argument(help="Логин: латиница, цифры, точка, дефис, подчёркивание.")],
    admin: Annotated[bool, typer.Option("--admin", help="Сделать администратором.")] = False,
) -> None:
    """Создать пользователя. Пароль спрашивается в консоли и не попадает в историю команд."""
    password = _ask_password()

    async def run() -> None:
        try:
            async with async_session() as session, session.begin():
                user = await UserService(session).create(username, password, is_admin=admin)
        except UserError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        role = "администратор" if user.is_admin else "пользователь"
        console.print(f"Создан {role} [bold]{user.username}[/bold]")

    _run(run())


@user_app.command("list")
def user_list() -> None:
    """Показать пользователей."""

    async def run() -> None:
        async with async_session() as session:
            users = await UserRepository(session).list_all()
        if not users:
            console.print("Пользователей пока нет. Создать: sito user create <логин> --admin")
            return
        table = Table()
        for header in ("логин", "роль", "активен", "последний вход"):
            table.add_column(header)
        for u in users:
            table.add_row(
                u.username,
                "администратор" if u.is_admin else "пользователь",
                "да" if u.is_active else "[dim]нет[/dim]",
                f"{u.last_login_at.astimezone():%Y-%m-%d %H:%M}" if u.last_login_at else "—",
            )
        console.print(table)

    _run(run())


@user_app.command("set-password")
def user_set_password(username: str) -> None:
    """Сменить пароль. Все прежние входы пользователя перестанут действовать."""
    password = _ask_password()

    async def run() -> None:
        async with async_session() as session, session.begin():
            user = await UserRepository(session).get_by_username(username.strip().lower())
            if user is None:
                console.print(f"[red]Пользователя {username} нет[/red]")
                raise typer.Exit(1)
            try:
                await UserService(session).set_password(user, password)
            except UserError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
        console.print(f"Пароль {user.username} изменён")

    _run(run())


@pipeline_app.command("recluster")
def pipeline_recluster(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Не спрашивать подтверждение.")] = False,
) -> None:
    """Склеить все посты в сюжеты заново: после смены правил или порогов склейки.

    Сюжеты удаляются и собираются заново. Посты и их векторы не трогаются.
    """
    if not yes:
        typer.confirm("Все сюжеты будут удалены и собраны заново. Продолжить?", abort=True)

    async def run() -> None:
        service = ClusteringService(
            async_session, ClusteringRules.from_settings(settings), batch_size=settings.cluster_batch_size
        )
        total = await service.reset()
        console.print(f"К пересклейке постов: {total}")
        attached = new_stories = errors = 0
        with console.status("Склейка...") as status:
            while (result := await service.process_batch()).total:
                attached += result.attached
                new_stories += result.new_stories
                errors += result.errors
                status.update(f"Склейка... {attached + new_stories + errors} из {total}")
        console.print(f"Готово: сюжетов {new_stories}, приклеено постов {attached}, ошибок {errors}")

    _run(run())


@pipeline_app.command("reprocess")
def pipeline_reprocess(
    source_type: Annotated[str, typer.Option("--type", help="Тип источников: telegram или rss.")] = "telegram",
    untitled: Annotated[bool, typer.Option("--untitled", help="Только посты без заголовка и отсеянные.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Не спрашивать подтверждение.")] = False,
) -> None:
    """Пропустить посты одного типа заново через весь конвейер: после смены правил очистки.

    Посты выйдут из сюжетов, worker заново очистит их, посчитает векторы и склеит. Сюжеты, где
    остались только такие посты, пропадут до пересклейки (вместе с отметками «прочитано»).
    """
    if not yes:
        typer.confirm(f"Посты типа {source_type} будут обработаны заново. Продолжить?", abort=True)

    async def run() -> None:
        async with async_session() as session, session.begin():
            total = await StoryRepository(session).reprocess(source_type, untitled=untitled)
        console.print(f"Отправлено в обработку постов: {total}. Worker обработает их в фоне.")

    _run(run())


@app.command("web")
def web(
    host: Annotated[str | None, typer.Option(help="Адрес, по умолчанию из WEB_HOST.")] = None,
    port: Annotated[int | None, typer.Option(help="Порт, по умолчанию из WEB_PORT.")] = None,
    reload: Annotated[bool, typer.Option(help="Перезапуск при изменении кода, для разработки.")] = False,
) -> None:
    """Запустить веб. За обратным прокси доверяет X-Forwarded-* от адресов из FORWARDED_ALLOW_IPS."""
    import uvicorn

    if settings.secret_key is None:
        console.print(
            "[red]Не задан SITO_SECRET_KEY.[/red] Сгенерируйте ключ и добавьте в .env:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
        raise typer.Exit(1)

    uvicorn.run(
        "sito.web.app:create_app",
        factory=True,
        host=host or settings.web_host,
        port=port or settings.web_port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,  # логи настраивает setup_logging, uvicorn не должен их перетирать
    )


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Запустить асинхронную команду и закрыть соединения с базой."""

    async def wrapper() -> None:
        try:
            await coro
        finally:
            await engine.dispose()

    asyncio.run(wrapper())


if __name__ == "__main__":
    app()
