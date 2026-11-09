"""Логи: токен RSS-ленты не попадает в журнал доступа; JSON — для Loki."""

import json
import logging
from datetime import datetime

from sito.log import HideRssToken, JsonFormatter


def test_rss_token_hidden_in_access_log() -> None:
    record = logging.makeLogRecord({
        "msg": '%s - "%s %s HTTP/%s" %d',
        "args": ("1.2.3.4:5", "GET", "/rss/s3cr3t-Token_1/digests?x=1", "1.1", 200),
    })

    assert HideRssToken().filter(record)
    assert record.getMessage() == '1.2.3.4:5 - "GET /rss/***/digests?x=1 HTTP/1.1" 200'


def test_json_log_for_loki() -> None:
    """Loki берёт из строки level (метка) и time (время записи), остальные поля ищутся через | json."""
    try:
        raise ValueError("битый HTML")
    except ValueError as exc:
        record = logging.makeLogRecord({
            "name": "sito.worker", "levelname": "ERROR", "levelno": logging.ERROR,
            "msg": "source=%s: ошибка", "args": (7,), "source_id": 7, "exc_info": (type(exc), exc, exc.__traceback__),
        })

    data = json.loads(JsonFormatter().format(record))

    assert (data["level"], data["logger"], data["message"], data["source_id"]) == ("ERROR", "sito.worker", "source=7: ошибка", 7)
    assert datetime.fromisoformat(data["time"]).tzinfo is not None
    assert "ValueError: битый HTML" in data["exception"]
