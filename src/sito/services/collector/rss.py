import asyncio
import calendar
from datetime import UTC, datetime
from http import HTTPStatus

import feedparser
from httpx import AsyncClient

from sito.models import Source
from sito.services.collector.base import BaseCollector, FetchResult, RawPost


class RssCollector(BaseCollector):
    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    async def fetch(self, source: Source) -> FetchResult:
        # Условный запрос: если лента не менялась, сервер ответит 304 без тела
        headers = {}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified

        resp = await self.client.get(source.url, headers=headers)
        if resp.status_code == HTTPStatus.NOT_MODIFIED:
            return FetchResult(etag=source.etag, last_modified=source.last_modified, not_modified=True)
        resp.raise_for_status()

        # Разбор большой ленты занимает процессор, уносим его из event loop
        feed = await asyncio.to_thread(feedparser.parse, resp.content)
        posts = [post for entry in feed.entries if (post := self._to_post(source.id, entry))]

        return FetchResult(
            posts=posts,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )

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
