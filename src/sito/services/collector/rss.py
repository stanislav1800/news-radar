import asyncio
import calendar
from datetime import UTC, datetime
from http import HTTPStatus

import feedparser
from httpx import AsyncClient

from sito.models import Source
from sito.services.collector.base import BaseCollector, FetchResult, RawPost
from sito.utils.http import get_limited

MAX_FEED_BYTES = 10_000_000  # больше лент не бывает; больше — что-то не то
FEED_TIMEOUT = 45           # на всю загрузку ленты (общий предел сбора источника — 60 с)


class RssCollector(BaseCollector):
    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def fetch(self, source: Source) -> FetchResult:
        if not source.url:
            raise ValueError(f"У RSS-источника {source.id} нет адреса ленты")
        # Условный запрос: если лента не менялась, сервер ответит 304 без тела.
        # Курсор RSS — ETag и Last-Modified из прошлого ответа
        state = source.state or {}
        headers = {}
        if etag := state.get("etag"):
            headers["If-None-Match"] = etag
        if last_modified := state.get("last_modified"):
            headers["If-Modified-Since"] = last_modified

        resp, content = await get_limited(self.client, source.url, headers=headers, max_bytes=MAX_FEED_BYTES,
                                          timeout=FEED_TIMEOUT)
        if resp.status_code == HTTPStatus.NOT_MODIFIED:
            return FetchResult(not_modified=True)
        resp.raise_for_status()

        # Разбор большой ленты занимает процессор, уносим его из event loop
        feed = await asyncio.to_thread(feedparser.parse, content or b"")
        if not feed.entries and (feed.bozo or not feed.get("version")):
            # 200, но не лента: заглушка антибота, страница ошибки. Это сбой, а не «новых записей нет»
            raise ValueError(f"ответ — не лента RSS/Atom ({resp.headers.get('content-type', 'тип не указан')})")
        posts = [post for entry in feed.entries if (post := self._to_post(source.id, entry))]

        new_state = {"etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}
        return FetchResult(posts=posts, state={k: v for k, v in new_state.items() if v})

    @classmethod
    def _to_post(cls, source_id: int, entry: feedparser.FeedParserDict) -> RawPost | None:
        url = entry.get("link")
        external_id = entry.get("id") or url
        if not url or not external_id:
            return None  # без адреса запись бесполезна, остальную ленту не роняем

        content = entry.get("content")
        text = content[0].get("value") if content else entry.get("summary")

        return RawPost(
            source_id=source_id,
            external_id=external_id,
            url=url,
            published_at=cls._to_datetime(entry),
            title=entry.get("title", ""),
            text=text or None,
        )

    @staticmethod
    def _to_datetime(entry: feedparser.FeedParserDict) -> datetime | None:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed:
            return None
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
