"""Разметка пар постов для подбора порогов склейки в сюжеты (шаг 5).

    uv run python scripts/label_pairs.py sample   # выбрать пары из базы в eval/pairs.csv
    uv run python scripts/label_pairs.py label    # разметить: 1 — то же событие, 2 — разные, 3 — не понять
    uv run python scripts/label_pairs.py report   # как ответы распределились по слоям сходства

Пары из одного источника — в отдельный файл:
    uv run python scripts/label_pairs.py sample --same-source --file eval/pairs_same_source.csv
    uv run python scripts/label_pairs.py label --file eval/pairs_same_source.csv
"""

import asyncio
import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import text

from sito.db.engine import engine

DEFAULT_FILE = Path(__file__).parent.parent / "eval" / "pairs.csv"
FIELDS = [
    "bucket", "sim", "post_a", "post_b", "source_a", "source_b",
    "published_a", "published_b", "title_a", "title_b", "text_a", "text_b", "label",
]
LABELS = {"1": "same", "2": "different", "3": "unsure"}
LABEL_NAMES = {"same": "то же событие", "different": "разные", "unsure": "не понять"}

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

# Пары из разных (или одного) источников, опубликованные не дальше двух суток друг от друга, по слоям сходства.
# Внутри слоя порядок псевдослучайный, но воспроизводимый (md5 от id пары).
SAMPLE_SQL = """
WITH p AS (
    SELECT id, source_id, embedding, coalesce(published_at, fetched_at) AS t
    FROM post WHERE status = 'embedded'
),
pairs AS (
    SELECT a.id AS post_a, b.id AS post_b, 1 - (a.embedding <=> b.embedding) AS sim
    FROM p a JOIN p b
      ON a.id < b.id AND a.source_id {source_op} b.source_id AND abs(extract(epoch FROM a.t - b.t)) < 172800
),
bucketed AS (
    SELECT *, CASE WHEN sim >= 0.94 THEN 0.94 ELSE floor(sim * 50) / 50 END AS bucket
    FROM pairs WHERE sim >= :min_sim
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY bucket ORDER BY md5(post_a::text || '-' || post_b::text)) AS rn
    FROM bucketed
)
SELECT r.bucket, r.sim, r.post_a, r.post_b,
       sa.name AS source_a, sb.name AS source_b,
       to_char(pa.published_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS published_a,
       to_char(pb.published_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS published_b,
       pa.title AS title_a, pb.title AS title_b,
       left(coalesce(pa.text, ''), 300) AS text_a, left(coalesce(pb.text, ''), 300) AS text_b
FROM ranked r
JOIN post pa ON pa.id = r.post_a JOIN source sa ON sa.id = pa.source_id
JOIN post pb ON pb.id = r.post_b JOIN source sb ON sb.id = pb.source_id
WHERE r.rn <= :per_bucket
ORDER BY md5(r.post_a::text || '-' || r.post_b::text)  -- слои вперемешку, чтобы не подстраиваться под сходство
"""


FileOption = Annotated[Path, typer.Option("--file", help="CSV с парами.")]


def read_pairs(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_pairs(path: Path, rows: list[dict[str, str]]) -> None:
    # Колонки берутся из самих строк: в файле могут быть добавленные вручную (например, label_a)
    fields = list(rows[0].keys()) if rows else FIELDS
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)  # атомарно: прерывание не испортит файл


def bucket_name(bucket: str) -> str:
    low = float(bucket)
    return "0.94+" if low >= 0.94 else f"{low:.2f}–{low + 0.02:.2f}"


@app.command()
def sample(
    per_bucket: Annotated[int, typer.Option(help="Пар из каждого слоя сходства.")] = 20,
    min_sim: Annotated[float, typer.Option(help="Нижняя граница сходства.")] = 0.84,
    same_source: Annotated[bool, typer.Option(help="Пары из одного источника вместо разных.")] = False,
    force: Annotated[bool, typer.Option(help="Перезаписать файл с уже размеченными парами.")] = False,
    file: FileOption = DEFAULT_FILE,
) -> None:
    """Выбрать пары из базы."""
    if file.exists() and not force:
        console.print(f"[red]{file} уже есть, в нём может быть разметка. Перезаписать: --force[/red]")
        raise typer.Exit(1)
    sql = text(SAMPLE_SQL.format(source_op="=" if same_source else "<>"))

    async def run() -> list[dict[str, str]]:
        try:
            async with engine.connect() as conn:
                result = await conn.execute(sql, {"min_sim": min_sim, "per_bucket": per_bucket})
                return [{k: "" if v is None else str(v) for k, v in row._mapping.items()} for row in result]
        finally:
            await engine.dispose()

    with console.status("Перебираю пары постов (около полуминуты)..."):
        rows = asyncio.run(run())
    for row in rows:
        row["sim"] = f"{float(row['sim']):.4f}"
        row["label"] = ""
    file.parent.mkdir(exist_ok=True)
    write_pairs(file, rows)
    console.print(f"Выбрано пар: {len(rows)} → {file}")


@app.command()
def label(file: FileOption = DEFAULT_FILE) -> None:
    """Разметить пары. Ответ сохраняется сразу, прервать можно в любой момент."""
    rows = read_pairs(file)
    todo = [r for r in rows if not r["label"]]
    if not todo:
        console.print("Все пары размечены. Итоги: report")
        return

    console.print(
        "[bold]Одно и то же событие?[/bold]  "
        "[green]1[/green] — да   [red]2[/red] — разные   [yellow]3[/yellow] — не понять   "
        "[dim]b — назад, q — выйти[/dim]\n"
        "Одно событие — одно происшествие, выступление, решение или релиз со всеми подробностями и реакциями.\n"
        "Тема может совпадать, а событие — нет: «Восток уничтожил 47 БПЛА» и «Запад уничтожил 83» — разные.\n"
    )
    history: list[dict[str, str]] = []
    i = 0
    while i < len(todo):
        row = todo[i]
        done = len(rows) - len(todo) + i
        console.rule(f"Пара {done + 1} из {len(rows)}")
        for side in ("a", "b"):
            body = f"[bold]{row[f'title_{side}']}[/bold]"
            if row[f"text_{side}"]:
                body += f"\n[dim]{row[f'text_{side}']}[/dim]"
            subtitle = f"{row[f'source_{side}']}  {row[f'published_{side}'] or 'время неизвестно'} UTC"
            console.print(Panel(body, subtitle=subtitle, subtitle_align="right"))

        key = click.getchar().lower()
        if key == "q":
            break
        if key == "b" and history:
            prev = history.pop()
            prev["label"] = ""
            write_pairs(file, rows)
            i -= 1
            continue
        if key not in LABELS:
            console.print("[dim]Нажми 1, 2, 3, b или q[/dim]")
            continue
        row["label"] = LABELS[key]
        write_pairs(file, rows)
        history.append(row)
        console.print(f"→ {LABEL_NAMES[row['label']]}\n")
        i += 1

    left = sum(1 for r in rows if not r["label"])
    console.print(f"Размечено {len(rows) - left} из {len(rows)}." + (" Продолжить: label" if left else " Итоги: report"))


@app.command()
def report(
    file: FileOption = DEFAULT_FILE,
    column: Annotated[str, typer.Option(help="Колонка с ответами, например label_a.")] = "label",
) -> None:
    """Сколько пар каждого вида в каждом слое сходства."""
    rows = [{**r, "label": r[column]} for r in read_pairs(file) if r.get(column)]
    if not rows:
        console.print("Размеченных пар пока нет")
        return

    by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_bucket[row["bucket"]][row["label"]] += 1

    table = Table(title=f"Размечено пар: {len(rows)}")
    for header in ("сходство", "то же", "разные", "не понять", "доля «то же»"):
        table.add_column(header, justify="right")
    for bucket in sorted(by_bucket, key=float):
        c = by_bucket[bucket]
        decided = c["same"] + c["different"]
        share = f"{c['same'] / decided:.0%}" if decided else "—"
        table.add_row(bucket_name(bucket), str(c["same"]), str(c["different"]), str(c["unsure"]), share)
    console.print(table)


if __name__ == "__main__":
    app()
