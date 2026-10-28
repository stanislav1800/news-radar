"""Консольные команды Сита.

    uv run sito source add "ТАСС" https://tass.ru/rss/v2.xml
    uv run sito source list
    uv run sito source disable 3
"""

import asyncio
from collections.abc import Coroutine
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from sito.config import settings
from sito.db.engine import async_session, engine
from sito.log import setup_logging
from sito.models import Source, SourceType
from sito.repositories.sources import SourceRepository
from sito.services.collector.base import FetchResult
from sito.services.collector.rss import RssCollector

app = typer.Typer(help="Сито: персональный новостной радар.", no_args_is_help=True)
source_app = typer.Typer(help="Источники: RSS-ленты и Telegram-каналы.", no_args_is_help=True)
app.add_typer(source_app, name="source")

console = Console()
PREVIEW_TITLES = 3


@app.callback()
def _setup() -> None:
    setup_logging(settings.log_level, settings.log_format, settings.log_sql)


async def _trial_fetch(url: str) -> FetchResult:
    """Пробный сбор без сохранения: проверить, что лента рабочая (по ТЗ)."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.http_user_agent},
    ) as client:
        return await RssCollector(client).fetch(Source(type=SourceType.RSS, name="", url=url))


@source_app.command("add")
def source_add(
    name: Annotated[str, typer.Argument(help="Название для людей, например «ТАСС».")],
    url: Annotated[str, typer.Argument(help="Адрес RSS-ленты или https://t.me/<канал>.")],
    type_: Annotated[SourceType, typer.Option("--type", "-t", help="Тип источника.")] = SourceType.RSS,
    interval: Annotated[int, typer.Option(min=60, help="Как часто опрашивать, секунд.")] = 300,
    check: Annotated[bool, typer.Option(help="Сделать пробный сбор перед сохранением.")] = True,
) -> None:
    """Добавить источник. Перед сохранением делает пробный сбор и показывает результат."""

    async def run() -> None:
        async with async_session() as session:
            if await SourceRepository(session).get_by_url(url):
                console.print(f"[red]Источник с адресом {url} уже есть[/red]")
                raise typer.Exit(1)

        if check and type_ is SourceType.RSS:
            with console.status("Пробный сбор..."):
                try:
                    result = await _trial_fetch(url)
                except (httpx.HTTPError, ValueError) as exc:
                    console.print(f"[red]Лента не читается: {type(exc).__name__}: {exc}[/red]")
                    console.print("Не сохранено. Добавить без проверки: --no-check")
                    raise typer.Exit(1) from exc
            if not result.posts:
                console.print("[red]Лента прочиталась, но постов в ней нет. Это точно RSS?[/red]")
                console.print("Не сохранено. Добавить без проверки: --no-check")
                raise typer.Exit(1)
            console.print(f"[green]Лента рабочая:[/green] постов {len(result.posts)}, например:")
            for post in result.posts[:PREVIEW_TITLES]:
                console.print(f"  • {post.title}")
        elif check:
            console.print(f"[yellow]Пробный сбор для типа {type_} пока не поддерживается[/yellow]")

        async with async_session() as session, session.begin():
            source = await SourceRepository(session).add(type_, name, url, interval)
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
                f"{s.poll_interval} с",
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
