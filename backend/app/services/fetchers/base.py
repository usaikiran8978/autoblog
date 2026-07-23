"""Fetcher contract + shared normalization helpers."""

from __future__ import annotations

import abc
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.db.models import Source

# Tracking params that make two identical URLs look different. Stripping them
# is what lets `url_hash` actually deduplicate on re-ingest.
_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|igshid|si$|__twitter)"
)


@dataclass(slots=True)
class FetchedItem:
    """Normalized shape every fetcher returns, regardless of upstream format."""

    title: str
    url: str
    description: str | None = None
    content: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    image_url: str | None = None
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    popularity: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(normalize_url(self.url).encode()).hexdigest()


def normalize_url(url: str) -> str:
    """Strip tracking params, fragments, trailing slash; lowercase the host."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


def clean_text(value: str | None, limit: int | None = None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)          # strip markup
    text = re.sub(r"\s+", " ", text).strip()        # collapse whitespace
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text or None


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class BaseFetcher(abc.ABC):
    """One fetcher per upstream shape (RSS, HN, Reddit, GitHub, PH, scrape)."""

    slug: str

    @abc.abstractmethod
    async def fetch(self, source: Source, client: httpx.AsyncClient) -> list[FetchedItem]:
        ...
