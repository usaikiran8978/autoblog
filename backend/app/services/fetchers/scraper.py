"""Article body extraction, used only where scraping is permitted.

Three guardrails, because scraping is the part of this system most likely to
get you blocked or sued:
  1. robots.txt is honoured when RESPECT_ROBOTS_TXT=true (the default).
  2. URLs are SSRF-checked before the request leaves the process.
  3. Only an excerpt is retained. Full-text copies of third-party articles are
     both a legal risk and useless to us — the writer needs enough to reason
     about the story, not a reproduction of it.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import SOURCE_ERRORS
from app.core.resilience import CircuitBreaker, RateLimiter
from app.core.security import assert_safe_url

log = get_logger(__name__)

# Keep enough for the model to work with, not enough to be a republication.
MAX_EXTRACT_CHARS = 6000

_robots_cache: dict[str, RobotFileParser | None] = {}
_robots_lock = asyncio.Lock()


async def _robots_allows(url: str, client: httpx.AsyncClient) -> bool:
    if not settings.RESPECT_ROBOTS_TXT:
        return True

    host = urlparse(url).netloc
    async with _robots_lock:
        if host not in _robots_cache:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = await client.get(urljoin(url, "/robots.txt"), timeout=8.0)
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())  # type: ignore[union-attr]
                else:
                    parser = None  # no robots.txt => unrestricted
            except httpx.HTTPError:
                parser = None
            _robots_cache[host] = parser

    parser = _robots_cache.get(host)
    if parser is None:
        return True
    return parser.can_fetch(settings.HTTP_USER_AGENT, url)


async def extract_article(url: str, client: httpx.AsyncClient) -> str | None:
    """Return a cleaned excerpt of the article body, or None if we may not or
    cannot fetch it. Never raises — extraction is best-effort enrichment."""
    if not settings.SCRAPE_ENABLED:
        return None

    host = urlparse(url).netloc
    breaker = CircuitBreaker(f"scrape:{host}")
    if await breaker.is_open():
        log.info("scrape_skipped_circuit_open", host=host)
        return None

    try:
        assert_safe_url(url)
    except ValueError as exc:
        log.warning("scrape_blocked_unsafe_url", url=url, reason=str(exc))
        return None

    if not await _robots_allows(url, client):
        log.info("scrape_disallowed_by_robots", url=url)
        return None

    # One request per second per host, shared across all workers.
    await RateLimiter(f"scrape:{host}", rate_per_sec=1.0, capacity=5).acquire()

    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        await breaker.record_failure()
        SOURCE_ERRORS.labels(host, "fetch").inc()
        log.warning("scrape_fetch_failed", url=url, error=str(exc))
        return None

    await breaker.record_success()

    # trafilatura is blocking and CPU-heavy; run it in a thread.
    text = await asyncio.to_thread(
        trafilatura.extract,
        resp.text,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text:
        return None

    text = text.strip()
    return text[:MAX_EXTRACT_CHARS] if len(text) > MAX_EXTRACT_CHARS else text
