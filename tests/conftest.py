"""Общие фикстуры.

Тесты с базой работают с отдельной базой <имя из DATABASE_URL>_test: она пересоздаётся
в начале прогона, а каждый тест выполняется в транзакции, которая в конце откатывается.
Рабочая база не трогается.
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI

ROOT = Path(__file__).parent.parent


def _test_database_url() -> URL:
    if explicit := os.environ.get("TEST_DATABASE_URL"):
        url = make_url(explicit)
    else:
        base = os.environ.get("DATABASE_URL") or dotenv_values(ROOT / ".env").get("DATABASE_URL")
        if not base:
            raise RuntimeError("Нет DATABASE_URL: задай его в .env или TEST_DATABASE_URL")
        url = make_url(base)
        if not (url.database or "").endswith("_test"):
            url = url.set(database=f"{url.database}_test")
    # Защита: тесты удаляют базу целиком, рабочую трогать нельзя
    if not (url.database or "").endswith("_test"):
        raise RuntimeError(f"Имя тестовой базы должно оканчиваться на _test: {url.database}")
    return url


TEST_DATABASE_URL = _test_database_url()
# До импорта sito: настройки и engine должны прочитать адрес тестовой базы
os.environ["DATABASE_URL"] = TEST_DATABASE_URL.render_as_string(hide_password=False)

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Connector, Source, SourceType


async def _recreate_database(url: URL) -> None:
    conn = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, database="postgres"
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[None]:
    """Чистая база с применёнными миграциями: заодно проверяем, что миграции работают."""
    await _recreate_database(TEST_DATABASE_URL)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", "head",
        cwd=ROOT, env=os.environ, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"Миграции не применились:\n{output.decode()}")
    yield
    from sito.db.engine import engine

    await engine.dispose()


@pytest.fixture
async def session_factory(database: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий поверх одной транзакции, которая откатывается после теста.

    `session.begin()` внутри кода превращается в SAVEPOINT, поэтому код под тестом
    коммитит как обычно, а в базе после теста ничего не остаётся.
    """
    from sito.db.engine import engine

    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield async_sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        await transaction.rollback()


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


MakeSource = Callable[..., Awaitable[int]]


@pytest.fixture
def make_source(session_factory: async_sessionmaker[AsyncSession]) -> MakeSource:
    """Создать источник и вернуть его id: await make_source(name="ТАСС", enabled=False)."""
    counter = 0

    async def make(**fields: object) -> int:
        nonlocal counter
        counter += 1
        async with session_factory() as session, session.begin():
            # Встроенное RSS-подключение создаёт миграция
            rss_connector = select(Connector.id).where(Connector.builtin, Connector.type == SourceType.RSS)
            values: dict[str, object] = {
                "connector_id": rss_connector.scalar_subquery(),
                "type": SourceType.RSS, "name": f"source {counter}", "url": f"https://feed{counter}.example/rss",
                "poll_interval": 300,
            }
            values.update(fields)
            return (await session.execute(insert(Source).values(**values).returning(Source.id))).scalar_one()

    return make


class FakeEmbedder:
    """Модель для тестов: вектор запроса задаёт тест (embedder.vectors["запрос"] = [...])."""

    name = "fake"

    def __init__(self) -> None:
        from sito.models.post import EMBEDDING_DIM

        self.dim = EMBEDDING_DIM
        self.vectors: dict[str, list[float]] = {}
        self.calls: list[str] = []

    async def embed(self, texts: "Sequence[str]") -> list[list[float]]:
        self.calls.extend(texts)
        fallback = [0.0] * (self.dim - 1) + [1.0]  # последняя ось: ни на что в тестах не похоже
        return [self.vectors.get(text, fallback) for text in texts]


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], embedder: FakeEmbedder) -> "FastAPI":
    from pydantic import SecretStr

    from sito.config import settings
    from sito.web.app import create_app

    return create_app(
        session_factory,
        # LLM из .env в тестах не нужна: они не должны ходить в настоящий сервис
        settings.model_copy(update={"secret_key": SecretStr("test-secret-key-0123"), "llm_api_key": None}),
        embedder,  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(app: "FastAPI") -> AsyncIterator["httpx.AsyncClient"]:
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def user(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Обычный пользователь ivan с паролем tests.web.PASSWORD."""
    from sito.services.users import UserService
    from tests.web import PASSWORD

    async with session_factory() as session, session.begin():
        await UserService(session).create("Ivan", PASSWORD)
    return "ivan"


@pytest.fixture
async def logged_in(client: "httpx.AsyncClient", user: str) -> "httpx.AsyncClient":
    """Клиент, который уже вошёл как user."""
    from tests.web import login

    assert (await login(client, user)).status_code == 303
    return client
