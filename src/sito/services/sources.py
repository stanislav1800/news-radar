"""Источники: пробный сбор, добавление, включение и удаление. Общее для веба и CLI."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from sito.config import Settings
from sito.models import Connector, Source, SourceType
from sito.repositories.sources import SourceRepository
from sito.services.collector.base import FetchResult
from sito.services.collector.registry import RssSourceForm
from sito.services.collector.rss import RssCollector

PREVIEW_TITLES = 3

TrialFetch = Callable[[str], Awaitable[FetchResult]]


class SourceError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


@dataclass(frozen=True, slots=True)
class TrialResult:
    posts: int
    titles: list[str]  # первые заголовки, чтобы было видно, что лента та


def http_trial_fetch(config: Settings) -> TrialFetch:
    """Пробный сбор RSS по сети, без сохранения."""

    async def fetch(url: str) -> FetchResult:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=config.http_timeout, headers={"User-Agent": config.http_user_agent}
        ) as client:
            return await RssCollector(client).fetch(Source(type=SourceType.RSS, name="", url=url))

    return fetch


class SourceService:
    def __init__(self, session: AsyncSession, trial_fetch: TrialFetch) -> None:
        self.repo = SourceRepository(session)
        self.trial_fetch = trial_fetch

    async def trial(self, url: str) -> TrialResult:
        """Пробный сбор (ТЗ: перед добавлением показать, что лента рабочая)."""
        try:
            result = await self.trial_fetch(url)
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            raise SourceError(f"Лента не читается: {type(exc).__name__}: {exc}") from exc
        if not result.posts:
            raise SourceError("Лента прочиталась, но постов в ней нет. Это точно RSS?")
        return TrialResult(len(result.posts), [p.title for p in result.posts[:PREVIEW_TITLES]])

    async def add_rss(
        self, connector: Connector, form: RssSourceForm, *, check: bool = True
    ) -> tuple[Source, TrialResult | None]:
        url = str(form.url)
        if await self.repo.get_by_url(url):
            raise SourceError(f"Источник с адресом {url} уже есть")
        trial = await self.trial(url) if check else None
        source = await self.repo.add(connector.id, connector.type, form.name, url=url, poll_interval=form.poll_interval)
        return source, trial
