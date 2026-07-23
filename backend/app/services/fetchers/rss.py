"""RSS/Atom fetcher — the default path for ~80% of configured sources.

Uses conditional GET (ETag / If-Modified-Since). A 304 costs one round trip
and zero parsing, which matters when polling 30 feeds twice a day.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import feedparser
import httpx

from app.core.logging_conf import get_logger
from app.db.models import Source
from app.services.fetchers.base import BaseFetcher, FetchedItem, clean_text, to_utc

log = get_logger(__name__)


class RSSFetcher(BaseFetcher):
    slug = "rss"

    async def fetch(self, source: Source, client: httpx.AsyncClient) -> list[FetchedItem]:
        headers: dict[str, str] = {}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified

        resp = await client.get(source.url, headers=headers)
        if resp.status_code == 304:
            log.info("feed_not_modified", source=source.slug)
            return []
        resp.raise_for_status()

        # Persist validators for the next poll.
        source.etag = resp.headers.get("ETag")
        source.last_modified = resp.headers.get("Last-Modified")

        # feedparser is CPU-bound and blocking; keep it off the event loop.
        parsed = await asyncio.to_thread(feedparser.parse, resp.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")

        items: list[FetchedItem] = []
        for entry in parsed.entries:
            link = entry.get("link")
            title = clean_text(entry.get("title"))
            if not link or not title:
                continue
            items.append(
                FetchedItem(
                    title=title,
                    url=link,
                    description=clean_text(
                        entry.get("summary") or entry.get("description"), limit=1200
                    ),
                    content=_entry_content(entry),
                    author=clean_text(entry.get("author")),
                    published_at=_entry_date(entry),
                    image_url=_entry_image(entry),
                    categories=list(source.categories or []),
                    tags=[t.get("term") for t in entry.get("tags", []) if t.get("term")][:8],
                    raw={"feed": source.slug, "id": entry.get("id")},
                )
            )
        return items


def _entry_content(entry) -> str | None:
    blocks = entry.get("content") or []
    if blocks and isinstance(blocks, list):
        return clean_text(blocks[0].get("value"), limit=8000)
    return None


def _entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        if struct := entry.get(key):
            try:
                return to_utc(datetime(*struct[:6], tzinfo=timezone.utc))
            except (TypeError, ValueError):
                continue
    return None


def _entry_image(entry) -> str | None:
    for media in entry.get("media_content", []) or []:
        if url := media.get("url"):
            return url
    for thumb in entry.get("media_thumbnail", []) or []:
        if url := thumb.get("url"):
            return url
    for link in entry.get("links", []) or []:
        if str(link.get("type", "")).startswith("image/"):
            return link.get("href")
    return None
