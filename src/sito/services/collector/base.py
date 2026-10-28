from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from sito.models import Source


@dataclass(frozen=True, slots=True)
class RawPost:
    """Пост как его отдал источник, без обработки."""

    source_id: int
    external_id: str
    url: str
    published_at: datetime | None
    title: str
    text: str | None  # HTML как пришёл, очистка на шаге 3


@dataclass(frozen=True, slots=True)
class FetchResult:
    posts: list[RawPost] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False  # сервер ответил 304: с прошлого раза ничего нового


class BaseCollector(ABC):
    @abstractmethod
    async def fetch(self, source: Source) -> FetchResult: ...
