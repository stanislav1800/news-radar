"""Подключения: создание и правка (веб) и работа слушающих сборщиков (worker)."""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sito.models import Connector, ConnectorStatus, SourceType
from sito.repositories.connectors import ConnectorRepository
from sito.services.collector.base import (
    ConnectorConfig,
    ConnectorListener,
    NeedsLogin,
    PostSink,
)
from sito.services.collector.registry import CollectorSpec
from sito.utils.crypto import SecretBox, SecretsError

logger = logging.getLogger(__name__)


class ConnectorError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


def secret_fields(form: type[BaseModel]) -> set[str]:
    return {name for name, info in form.model_fields.items() if info.annotation in (SecretStr, SecretStr | None)}


class ConnectorService:
    """Подключения из веба: учётные данные шифруются, в настройки попадает только несекретное."""

    def __init__(self, session: AsyncSession, box: SecretBox | None) -> None:
        self.session = session
        self.repo = ConnectorRepository(session)
        self.box = box

    async def create(self, spec: CollectorSpec, form: BaseModel) -> Connector:
        settings, secrets = self._split(form)
        connector = Connector(
            type=spec.source_type, name=str(settings.pop("name")), builtin=False,
            settings=settings, secrets=self.encrypt(secrets),
            # Подключение со входом (Telegram) заработает после мастера входа
            status=ConnectorStatus.NEEDS_LOGIN if spec.login else ConnectorStatus.ACTIVE,
        )
        self.session.add(connector)
        await self.session.flush()
        return connector

    def current_secrets(self, connector: Connector) -> dict[str, Any]:
        """Сохранённые секреты: при правке пустое поле секрета значит «оставить прежнее»."""
        if connector.secrets is None:
            return {}
        if self.box is None:
            raise ConnectorError("Не задан SITO_SECRET_KEY: секреты подключения не расшифровать")
        try:
            return self.box.decrypt(connector.secrets)
        except SecretsError as exc:
            raise ConnectorError(str(exc)) from exc

    async def update(self, connector: Connector, form: BaseModel, *, login: bool = False) -> None:
        """Новые настройки и секреты. Worker заметит изменение и переподключится.

        Секреты, которых нет в форме (сессия после входа), сохраняются, пока учётные данные те же.
        login — у подключения есть вход: при смене учётных данных нужно войти заново.
        """
        settings, secrets = self._split(form)
        connector.name = str(settings.pop("name"))
        current = self.current_secrets(connector)
        same_account = (
            all(connector.settings.get(k) == v for k, v in settings.items())
            and all(current.get(k) == v for k, v in secrets.items())
        )
        kept = {k: v for k, v in current.items() if k not in secrets} if same_account else {}
        # Настройки не из формы (заданы на странице каналов) сохраняются
        connector.settings = {**{k: v for k, v in connector.settings.items() if k not in settings}, **settings}
        connector.secrets = self.encrypt({**kept, **secrets})
        if login and not same_account:
            connector.status = ConnectorStatus.NEEDS_LOGIN
            connector.last_error = "Учётные данные изменились: войдите заново"
        elif not login or connector.status != ConnectorStatus.NEEDS_LOGIN:
            connector.status = ConnectorStatus.ACTIVE
            connector.last_error = None

    async def set_enabled(self, connector: Connector, enabled: bool) -> None:
        if connector.builtin and not enabled:
            raise ConnectorError("Встроенное подключение не выключается: выключайте отдельные источники")
        connector.enabled = enabled

    async def delete(self, connector: Connector) -> None:
        if connector.builtin:
            raise ConnectorError("Встроенное подключение удалить нельзя")
        if await self.repo.has_sources(connector.id):
            # FK RESTRICT: вместе с источниками пропали бы все их посты
            raise ConnectorError(
                "У подключения есть источники. Удалите их или просто выключите подключение — посты сохранятся."
            )
        await self.session.delete(connector)

    async def set_options(self, connector: Connector, **options: Any) -> None:
        """Настройки не из формы подключения (например, что делать с новыми каналами). Worker переподключится."""
        connector.settings = {**connector.settings, **options}

    @staticmethod
    def _split(form: BaseModel) -> tuple[dict[str, Any], dict[str, Any]]:
        secret_names = secret_fields(type(form))
        settings: dict[str, Any] = {}
        secrets: dict[str, Any] = {}
        for name, value in form.model_dump(mode="json").items():
            if name in secret_names:
                raw = getattr(form, name)
                if raw is not None:
                    secrets[name] = raw.get_secret_value()
            else:
                settings[name] = value
        return settings, secrets

    def encrypt(self, secrets: Mapping[str, Any]) -> bytes | None:
        if not secrets:
            return None
        if self.box is None:
            raise ConnectorError("Не задан SITO_SECRET_KEY: без него секреты подключения не сохранить")
        return self.box.encrypt(secrets)


# --- Worker: слушающие подключения ---------------------------------------------------------------


def fingerprint(connector: Connector) -> str:
    """Отпечаток настроек: изменился — подключение надо перезапустить. Статус и ошибки не входят."""
    digest = hashlib.sha256()
    digest.update(connector.type.encode())
    digest.update(json.dumps(connector.settings, sort_keys=True, ensure_ascii=False).encode())
    digest.update(connector.secrets or b"")
    return digest.hexdigest()


@dataclass
class _Running:
    task: asyncio.Task[None]
    fingerprint: str
    started_at: float
    failures: int
    checked_at: float = 0.0  # когда сторож последний раз смотрел, идут ли посты


# Когда пришёл последний пост подключения: среди свежих постов (по id), чтобы не перебирать всю таблицу.
# Нет среди последних 3000 — значит, давно (у живого Telegram сотни постов в час)
_LAST_POST_SQL = text("""
    SELECT max(recent.fetched_at) FROM (SELECT source_id, fetched_at FROM post ORDER BY id DESC LIMIT 3000) AS recent
    JOIN source ON source.id = recent.source_id WHERE source.connector_id = :connector_id
""")
_ENABLED_SOURCES_SQL = text("SELECT count(*) FROM source WHERE connector_id = :connector_id AND enabled")


@dataclass
class _Halted:
    fingerprint: str
    retry_at: float | None  # None — ждать, пока администратор не поменяет подключение
    failures: int


class ConnectorSupervisor:
    """Держит по задаче на каждое включённое слушающее подключение.

    Раз в круг сверяет подключения в базе со своими: новые запускает, выключенные и удалённые
    останавливает, изменённые перезапускает. Упавшее подключение перезапускается с нарастающей
    паузой; «нужен повторный вход» и нерасшифровываемые секреты — только после правки подключения.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        listeners: Mapping[SourceType, ConnectorListener],
        sink: PostSink,
        box: SecretBox | None,
        *,
        retry_min: float = 30,
        retry_max: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.listeners = listeners
        self.sink = sink
        self.box = box
        self.retry_min = retry_min
        self.retry_max = retry_max
        self.clock = clock
        self._running: dict[int, _Running] = {}
        self._halted: dict[int, _Halted] = {}
        self._types: dict[int, SourceType] = {}
        self._stopping: dict[int, asyncio.Task[None]] = {}  # отменённые, но ещё не завершившиеся

    @property
    def running(self) -> set[int]:
        return {cid for cid, r in self._running.items() if not r.task.done()}

    async def sync(self) -> None:
        if not self.listeners:
            return
        async with self.session_factory() as session:
            connectors = await ConnectorRepository(session).list_enabled(list(self.listeners))
        wanted = {c.id: c for c in connectors}

        for connector_id in list(self._running):
            if connector_id not in wanted:
                await self._stop(connector_id)
                logger.info("connector=%s: остановлено (выключено или удалено)", connector_id)
        for connector_id in list(self._halted):
            if connector_id not in wanted:
                del self._halted[connector_id]

        for connector in connectors:
            await self._sync_one(connector, fingerprint(connector))

    async def _sync_one(self, connector: Connector, current: str) -> None:
        stopping = self._stopping.get(connector.id)
        if stopping is not None:
            if not stopping.done():
                return
            del self._stopping[connector.id]
        running = self._running.get(connector.id)
        if running is not None:
            if not running.task.done():
                if running.fingerprint == current:
                    if not await self._silent(connector.id, running):
                        return
                    # Зависло что-то внутри подключения (запрос без ответа, оборванное соединение без
                    # ошибки): посты не идут — перезапустить. Так было 3–5.11: 39 часов без постов Telegram
                    logger.warning("connector=%s: новых постов нет дольше %s — перезапуск",
                                   connector.id, self.listeners[connector.type].silence_limit)
                    await self._stop(connector.id)
                    await self._set_status(connector.id, ConnectorStatus.ERROR,
                                           "Посты перестали приходить — подключение перезапущено")
                    if connector.id not in self._stopping:
                        await self._start(connector, current, running.failures + 1)
                    return
                logger.info("connector=%s: настройки изменились, переподключение", connector.id)
                await self._stop(connector.id)
                if connector.id in self._stopping:
                    return
            else:
                await self._finished(connector.id, running)

        halted = self._halted.get(connector.id)
        failures = 0
        if halted is not None:
            if halted.fingerprint == current:
                if halted.retry_at is None or self.clock() < halted.retry_at:
                    return
                failures = halted.failures
            del self._halted[connector.id]  # пора повторить или подключение поправили
        await self._start(connector, current, failures)

    async def _silent(self, connector_id: int, running: _Running) -> bool:
        """Подключение работает дольше silence_limit, а постов от него нет столько же. Смотрит раз в минуту."""
        listener = self.listeners[self._types.get(connector_id, SourceType.TELEGRAM)]
        limit = listener.silence_limit
        now = self.clock()
        if limit is None or now - running.started_at < limit.total_seconds() or now - running.checked_at < 60:
            return False
        running.checked_at = now
        async with self.session_factory() as session:
            enabled = await session.scalar(_ENABLED_SOURCES_SQL, {"connector_id": connector_id}) or 0
            if enabled < listener.watch_sources:
                return False
            last = await session.scalar(_LAST_POST_SQL, {"connector_id": connector_id})
        return last is None or datetime.now(UTC) - last > limit

    async def _start(self, connector: Connector, current: str, failures: int) -> None:
        secrets: dict[str, Any] = {}
        try:
            if connector.secrets:
                if self.box is None:
                    raise SecretsError("Не задан SITO_SECRET_KEY: секреты подключения не расшифровать")
                secrets = self.box.decrypt(connector.secrets)
        except SecretsError as exc:
            await self._set_status(connector.id, ConnectorStatus.ERROR, str(exc))
            self._halted[connector.id] = _Halted(current, None, failures)
            return
        config = ConnectorConfig(
            id=connector.id, type=connector.type, name=connector.name,
            settings=dict(connector.settings), secrets=secrets,
        )
        listener = self.listeners[connector.type]
        self._types[connector.id] = connector.type
        task = asyncio.create_task(listener.run(config, self.sink), name=f"connector-{connector.id}")
        self._running[connector.id] = _Running(task, current, self.clock(), failures)
        logger.info("connector=%s: запущено (%s)", connector.id, connector.name)

    async def _finished(self, connector_id: int, running: _Running) -> None:
        """Задача подключения завершилась сама: разобрать, почему, и решить, когда повторять."""
        del self._running[connector_id]
        exc = running.task.exception() if not running.task.cancelled() else None
        if isinstance(exc, NeedsLogin):
            logger.warning("connector=%s: нужен повторный вход: %s", connector_id, exc)
            await self._set_status(connector_id, ConnectorStatus.NEEDS_LOGIN, str(exc) or "Нужен повторный вход")
            self._halted[connector_id] = _Halted(running.fingerprint, None, 0)
            return
        # Проработало дольше самой длинной паузы — значит, сбой новый, а не череда подряд
        long_run = self.clock() - running.started_at > self.retry_max
        failures = 1 if long_run else running.failures + 1
        delay = min(self.retry_max, self.retry_min * 2 ** (failures - 1))
        error = f"{type(exc).__name__}: {exc}" if exc else "Соединение завершилось"
        logger.warning(
            "connector=%s: сбой (%s), повтор через %.0f с", connector_id, error, delay,
            exc_info=exc, extra={"connector_id": connector_id},
        )
        await self._set_status(connector_id, ConnectorStatus.ERROR, error)
        self._halted[connector_id] = _Halted(running.fingerprint, self.clock() + delay, failures)

    async def _stop(self, connector_id: int) -> None:
        running = self._running.pop(connector_id)
        running.task.cancel()
        # Ждать остановки не дольше 30 секунд: зависшее подключение могло не отпустить сразу
        done, _ = await asyncio.wait({running.task}, timeout=30)
        if not done:
            # Второй клиент с той же сессией Telegram может её отозвать: новое — только когда старое закончится
            logger.warning("connector=%s: не остановилось за 30 с, новое запущу после него", connector_id)
            self._stopping[connector_id] = running.task
        elif not running.task.cancelled() and (exc := running.task.exception()) is not None:
            logger.error("connector=%s: ошибка при остановке", connector_id, exc_info=exc)

    async def _set_status(self, connector_id: int, status: ConnectorStatus, error: str | None) -> None:
        async with self.session_factory() as session, session.begin():
            await ConnectorRepository(session).set_status(connector_id, status, error)

    async def run(self, stop: asyncio.Event, tick: float) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.sync()
                except Exception:
                    logger.exception("ошибка круга подключений")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=tick)
                except TimeoutError:
                    pass
        finally:
            await self.close()

    async def close(self) -> None:
        for connector_id in list(self._running):
            await self._stop(connector_id)

