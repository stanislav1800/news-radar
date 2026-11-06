"""Логи: токен RSS-ленты не попадает в журнал доступа."""

import logging

from sito.log import HideRssToken


def test_rss_token_hidden_in_access_log() -> None:
    record = logging.makeLogRecord({
        "msg": '%s - "%s %s HTTP/%s" %d',
        "args": ("1.2.3.4:5", "GET", "/rss/s3cr3t-Token_1/digests?x=1", "1.1", 200),
    })

    assert HideRssToken().filter(record)
    assert record.getMessage() == '1.2.3.4:5 - "GET /rss/***/digests?x=1 HTTP/1.1" 200'
