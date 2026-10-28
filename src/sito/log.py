import json
import logging
import logging.config
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


def setup_logging(level: LogLevel = "INFO", fmt: LogFormat = "text", sql: bool = False) -> None:
    logging.config.dictConfig({
        "version": 1,
        # Не выключать логгеры, созданные при импорте модулей до вызова setup_logging
        "disable_existing_loggers": False,
        "formatters": {
            "text": {"format": TEXT_FORMAT, "datefmt": "%Y-%m-%d %H:%M:%S"},
            "json": {"()": JsonFormatter},
        },
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
        },
    })
