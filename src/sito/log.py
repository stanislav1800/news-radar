import json
import logging
import logging.config
import re
from datetime import UTC, datetime
from typing import Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["text", "json"]

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Поля, которые есть у любой записи. Всё остальное пришло через extra={...}
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись: удобно для Docker и сборщиков логов (Loki, ELK)."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Поля из extra= становятся отдельными ключами: по ним можно фильтровать
        data.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


_RSS_TOKEN = re.compile(r"(/rss/)[^/?\s]+")


class HideRssToken(logging.Filter):
    """Токен в адресе RSS-ленты — как пароль: кто его знает, читает темы пользователя. В логи доступа не пишем."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_RSS_TOKEN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


def setup_logging(level: LogLevel = "INFO", fmt: LogFormat = "text", sql: bool = False) -> None:
    logging.config.dictConfig({
        "version": 1,
        # Не выключать логгеры, созданные при импорте модулей до вызова setup_logging
        "disable_existing_loggers": False,
        "formatters": {
            "text": {"format": TEXT_FORMAT, "datefmt": "%Y-%m-%d %H:%M:%S"},
            "json": {"()": JsonFormatter},
        },
        "filters": {"hide_rss_token": {"()": HideRssToken}},
        "handlers": {
            # В stderr: в Docker логи собирает сам Docker, файлы не нужны
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": fmt,
            },
        },
        "root": {"level": level, "handlers": ["console"]},
        "loggers": {
            # Шумные библиотеки: иначе строка на каждый HTTP-запрос
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "httpx2": {"level": "WARNING"},  # им пользуется huggingface_hub при скачивании моделей
            # INFO у sqlalchemy.engine печатает каждый SQL-запрос
            "sqlalchemy.engine": {"level": "INFO" if sql else "WARNING"},
            "uvicorn.access": {"filters": ["hide_rss_token"]},
        },
    })
