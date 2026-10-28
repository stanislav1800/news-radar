"""RssCollector без сети: ответы сервера подменяет httpx.MockTransport."""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from sito.models import Source, SourceType
from sito.services.collector.rss import RssCollector

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Тестовая лента</title>
  <item>
    <title>Добыча угля в Кузбассе выросла</title>
    <link>https://news.example/coal?utm_source=rss</link>
    <guid>coal-1</guid>
    <pubDate>Sat, 24 Oct 2026 14:17:00 +0300</pubDate>
    <description>&lt;p&gt;Анонс&amp;nbsp;новости&lt;/p&gt;</description>
  </item>
  <item>
    <title>Без guid: id берётся из ссылки</title>
    <link>https://news.example/no-guid</link>
  </item>
  <item>
    <title>Без ссылки: запись пропускается</title>
    <!-- isPermaLink="false": иначе по стандарту guid считается ссылкой на статью -->
    <guid isPermaLink="false">no-link</guid>
  </item>
</channel>
</rss>"""


def make_collector(handler: Callable[[httpx.Request], httpx.Response]) -> RssCollector:
    return RssCollector(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def make_source(**fields: object) -> Source:
    values = {"id": 7, "type": SourceType.RSS, "name": "test", "url": "https://news.example/rss"}
    values.update(fields)
    return Source(**values)


async def test_parses_entries() -> None:
    collector = make_collector(lambda request: httpx.Response(200, text=FEED))

    result = await collector.fetch(make_source())

    assert not result.not_modified
    assert [p.external_id for p in result.posts] == ["coal-1", "https://news.example/no-guid"]
    first = result.posts[0]
    assert first.source_id == 7
    assert first.title == "Добыча угля в Кузбассе выросла"
    assert first.url == "https://news.example/coal?utm_source=rss"  # нормализация не здесь
    assert first.published_at == datetime(2026, 10, 24, 11, 17, tzinfo=UTC)  # +03:00 → UTC
    assert first.text is not None and "Анонс" in first.text
    assert result.posts[1].published_at is None


async def test_returns_cache_headers() -> None:
    collector = make_collector(
        lambda request: httpx.Response(200, text=FEED, headers={"ETag": '"v2"', "Last-Modified": "Sat, 24 Oct 2026"})
    )

    result = await collector.fetch(make_source())

    assert result.etag == '"v2"'
    assert result.last_modified == "Sat, 24 Oct 2026"


async def test_conditional_request_and_304() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["etag"] = request.headers.get("If-None-Match")
        seen["modified"] = request.headers.get("If-Modified-Since")
        return httpx.Response(304)

    collector = make_collector(handler)
    result = await collector.fetch(make_source(etag='"v1"', last_modified="Fri, 23 Oct 2026"))

    assert seen == {"etag": '"v1"', "modified": "Fri, 23 Oct 2026"}
    assert result.not_modified
    assert result.posts == []
    assert result.etag == '"v1"'  # прежние значения сохраняются


async def test_server_error_raises() -> None:
    collector = make_collector(lambda request: httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await collector.fetch(make_source())
