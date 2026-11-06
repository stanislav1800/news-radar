"""Модели и миграции совпадают: индекс только в миграции следующий autogenerate молча предложил бы удалить."""

import asyncio
import os
import sys

from tests.conftest import ROOT


async def test_models_match_migrations(database: None) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "check",
        cwd=ROOT, env=os.environ, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()
