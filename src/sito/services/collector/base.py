"""Сборщики: как система получает посты из источников любого типа (шаг 1 пайплайна).

Сборщики бывают двух видов:

- опрашивающий (BaseCollector, RSS): worker по расписанию просит у него новое для одного источника;
- слушающий (ConnectorListener, Telegram): держит соединение подключения, сам получает новые
  посты и сам заводит источники (каналы), с которых они пришли.

Остальная система не знает, как устроен источник: она получает RawPost и сохраняет их через PostSink.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sito.models import Source, SourceType


@dataclass(frozen=True, slots=True)
class RawPost:
    """Пост как его отдал источник, без обработки."""

    source_id: int
    external_id: str
    url: str
    published_at: datetime | None
    title: str        # пустой, если у источника заголовков нет (Telegram): его достанет очистка
    text: str | None  # HTML как пришёл, очистка на шаге 3


@dataclass(frozen=True, slots=True)
class FetchResult:
    posts: list[RawPost] = field(default_factory=list)
    # Новый курсор источника (source.state); None — не менять
    state: dict[str, Any] | None = None
    not_modified: bool = False  # с прошлого раза ничего нового (HTTP 304)


class BaseCollector(ABC):
    """Опрашивающий сборщик: читает новое из одного источника."""

    @abstractmethod
    async def fetch(self, source: Source) -> FetchResult: ...


# --- Слушающие сборщики ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    """Подключение для сборщика: без объектов базы, с расшифрованными секретами."""

    id: int
    type: SourceType
    name: str
    settings: Mapping[str, Any]
    secrets: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class KnownSource:
    id: int
    external_id: str
    name: str
    enabled: bool
    state: Mapping[str, Any]
    info: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Command:
    """Поручение из веба (ConnectorCommand): показать канал по ссылке, отписаться."""

    id: int
    kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PendingSource:
    """Источник, добавленный ссылкой, на который подключение ещё не подписалось (нет external_id)."""

    id: int
    url: str


@dataclass(frozen=True, slots=True)
class SaveResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0  # статья уже есть у другого поста
    invalid: int = 0     # без нормального адреса

    def __str__(self) -> str:
        return (
            f"новых={self.inserted} обновлено={self.updated} без изменений={self.unchanged} "
            f"дублей={self.duplicates} без адреса={self.invalid}"
        )


class PostSink(Protocol):
    """Куда слушающий сборщик отдаёт посты. Реализация — IngestService."""

    async def sources(self, connector_id: int) -> list[KnownSource]:
        """Источники подключения: при старте — откуда догружать пропущенное (по state)."""
        ...

    async def source(
        self, connector_id: int, external_id: str, *, name: str, url: str | None = None, enabled: bool = True,
    ) -> KnownSource:
        """Источник по его id у сервиса; если его ещё нет — завести (первый пост с нового канала).

        Если источник уже завело другое подключение того же типа (канал читают два аккаунта),
        вернётся он же: посты не задваиваются.
        """
        ...

    async def save(
        self, source_id: int, posts: Sequence[RawPost], state: Mapping[str, Any] | None = None
    ) -> SaveResult:
        """Сохранить посты источника и, в той же транзакции, его новый курсор."""
        ...

    async def connected(self, connector_id: int) -> None:
        """Подключение работает: снять с него прежнюю ошибку."""
        ...

    async def pending_sources(self, connector_id: int) -> list[PendingSource]:
        """Источники, добавленные ссылкой, на которые ещё надо подписаться (старые первыми, без ошибки)."""
        ...

    async def bind_pending(self, source_id: int, external_id: str, *, name: str) -> KnownSource:
        """Подписались: источнику по ссылке — id у сервиса и название.

        Если такой канал уже есть (аккаунт был подписан раньше), ссылка просто удаляется
        и возвращается существующий источник.
        """
        ...

    async def pending_failed(self, source_id: int, error: str) -> None:
        """Подписаться не вышло (нет такого канала): ошибка видна в админке, повторов нет."""
        ...

    async def update_info(self, source_id: int, info: Mapping[str, Any]) -> None:
        """Сведения об источнике для админки (подписчики, описание, приглушён ли)."""
        ...

    async def forget_source(self, source_id: int, *, delete: bool) -> None:
        """Подключение отписалось: источник выключается (посты остаются) или удаляется вместе с постами."""
        ...

    async def next_command(self, connector_id: int) -> Command | None:
        """Следующее поручение из веба или None."""
        ...

    async def finish_command(self, command_id: int, result: Any = None, error: str | None = None) -> None:
        ...


class NeedsLogin(Exception):
    """Учётные данные подключения больше не действуют (сессию отозвали).

    Worker не перезапускает такое подключение, пока администратор не войдёт заново.
    """


class ConnectorListener(ABC):
    """Слушающий сборщик: держит соединение одного подключения.

    run работает, пока его не отменят (asyncio.CancelledError — штатная остановка).
    Возврат или исключение — сбой: worker перезапустит подключение с нарастающей паузой,
    а NeedsLogin — не перезапустит до повторного входа.
    """

    @abstractmethod
    async def run(self, connector: ConnectorConfig, sink: PostSink) -> None: ...
