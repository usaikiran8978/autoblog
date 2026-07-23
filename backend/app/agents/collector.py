"""Agent 1 — News Collector.

Fans out across every enabled source concurrently, normalizes the results into
`raw_articles`, and enriches the top candidates with body text and LLM
classification.

Two decisions worth calling out:

* Source failures are isolated. One dead feed produces a logged error and a
  `consecutive_failures` bump, never a failed run. Losing 1 of 30 sources is
  not a reason to skip an edition.
* Insert is `ON CONFLICT (url_hash) DO NOTHING`. Collection is idempotent, so
  a retried task cannot create duplicate rows.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import ARTICLES_COLLECTED, SOURCE_ERRORS
from app.core.resilience import CircuitBreaker, http_client
from app.db.models import RawArticle, Source, SourceKind
from app.llm.factory import get_provider
from app.prompts.templates import CLASSIFIER_SCHEMA, CLASSIFIER_SYSTEM
from app.services.fetchers.api_sources import (
    GitHubTrendingFetcher,
    HackerNewsFetcher,
    ProductHuntFetcher,
    RedditFetcher,
)
from app.services.fetchers.base import FetchedItem, normalize_url
from app.services.fetchers.rss import RSSFetcher
from app.services.fetchers.scraper import extract_article
from app.agents.base import Agent, AgentContext, record_cost

log = get_logger(__name__)

FETCHERS = {
    "rss": RSSFetcher(),
    "hackernews": HackerNewsFetcher(),
    "reddit": RedditFetcher(),
    "github_trending": GitHubTrendingFetcher(),
    "producthunt": ProductHuntFetcher(),
}

# How many of the freshest items get full-text extraction + classification.
# Enriching everything would triple the collection bill for items the ranker
# is going to discard anyway.
ENRICH_TOP_N = 60
CLASSIFY_BATCH = 20


@dataclass(slots=True)
class CollectionResult:
    article_ids: list[str]
    collected: int
    skipped_duplicates: int
    failed_sources: list[str]


class CollectorAgent(Agent[None, CollectionResult]):
    name = "collector"
    optional = False

    async def execute(self, ctx: AgentContext, payload: None = None) -> CollectionResult:
        sources = (
            await ctx.db.execute(select(Source).where(Source.enabled.is_(True)))
        ).scalars().all()
        if not sources:
            raise RuntimeError("no enabled sources — run `make seed` first")

        log.info("collection_started", sources=len(sources))

        async with http_client() as client:
            results = await asyncio.gather(
                *(self._fetch_source(s, client) for s in sources),
                return_exceptions=True,
            )

        items: list[tuple[Source, FetchedItem]] = []
        failed: list[str] = []
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                failed.append(source.slug)
                continue
            items.extend((source, item) for item in result)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.COLLECT_LOOKBACK_HOURS)
        fresh = [
            (s, i) for s, i in items
            if i.published_at is None or i.published_at >= cutoff
        ]

        inserted_ids = await self._persist(ctx, fresh)
        await self._enrich(ctx, inserted_ids)

        log.info(
            "collection_completed",
            fetched=len(items),
            fresh=len(fresh),
            inserted=len(inserted_ids),
            failed_sources=failed,
        )
        return CollectionResult(
            article_ids=[str(i) for i in inserted_ids],
            collected=len(inserted_ids),
            skipped_duplicates=len(fresh) - len(inserted_ids),
            failed_sources=failed,
        )

    # ------------------------------------------------------------------ fetch
    async def _fetch_source(
        self, source: Source, client: httpx.AsyncClient
    ) -> list[FetchedItem]:
        breaker = CircuitBreaker(f"source:{source.slug}")
        if await breaker.is_open():
            log.info("source_skipped_circuit_open", source=source.slug)
            return []

        fetcher_key = (
            (source.config or {}).get("fetcher", "rss")
            if source.kind != SourceKind.rss
            else "rss"
        )
        fetcher = FETCHERS.get(fetcher_key)
        if not fetcher:
            log.warning("unknown_fetcher", source=source.slug, fetcher=fetcher_key)
            return []

        try:
            items = await fetcher.fetch(source, client)
        except Exception as exc:
            await breaker.record_failure()
            source.consecutive_failures += 1
            source.last_error = f"{type(exc).__name__}: {exc}"[:500]
            SOURCE_ERRORS.labels(source.slug, type(exc).__name__).inc()
            log.warning("source_fetch_failed", source=source.slug, error=str(exc))
            # Auto-disable a feed that has been broken for a week of runs.
            if source.consecutive_failures >= 10:
                source.enabled = False
                log.error("source_auto_disabled", source=source.slug)
            raise

        await breaker.record_success()
        source.consecutive_failures = 0
        source.last_error = None
        source.last_fetched_at = datetime.now(timezone.utc)

        items = items[: settings.MAX_ITEMS_PER_SOURCE]
        ARTICLES_COLLECTED.labels(source.slug).inc(len(items))
        return items

    # ---------------------------------------------------------------- persist
    async def _persist(
        self, ctx: AgentContext, pairs: list[tuple[Source, FetchedItem]]
    ) -> list:
        if not pairs:
            return []

        # Collapse within-batch duplicates before touching the DB.
        by_hash: dict[str, tuple[Source, FetchedItem]] = {}
        for source, item in pairs:
            by_hash.setdefault(item.url_hash, (source, item))

        rows = [
            {
                "source_id": source.id,
                "title": item.title[:1000],
                "url": item.url,
                "url_hash": item.url_hash,
                "canonical_url": normalize_url(item.url),
                "description": item.description,
                "content": item.content,
                "author": item.author,
                "image_url": item.image_url,
                "published_at": item.published_at,
                "categories": item.categories,
                "tags": item.tags,
                "popularity_raw": item.popularity,
                "social_score": _social_score(item.popularity),
                "raw_payload": item.raw,
            }
            for source, item in by_hash.values()
        ]

        stmt = (
            pg_insert(RawArticle)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["url_hash"])
            .returning(RawArticle.id)
        )
        return list((await ctx.db.execute(stmt)).scalars().all())

    # ----------------------------------------------------------------- enrich
    async def _enrich(self, ctx: AgentContext, article_ids: list) -> None:
        """Body extraction + LLM classification for the freshest N items."""
        if not article_ids:
            return

        articles = (
            await ctx.db.execute(
                select(RawArticle)
                .where(RawArticle.id.in_(article_ids))
                .order_by(RawArticle.published_at.desc().nullslast())
                .limit(ENRICH_TOP_N)
            )
        ).scalars().all()

        # Only scrape items that arrived without usable body text.
        need_body = [a for a in articles if not a.content and a.url]
        if need_body:
            async with http_client() as client:
                bodies = await asyncio.gather(
                    *(extract_article(a.url, client) for a in need_body),
                    return_exceptions=True,
                )
            for article, body in zip(need_body, bodies, strict=True):
                if isinstance(body, str) and body:
                    article.content = body

        await self._classify(ctx, articles)

    async def _classify(self, ctx: AgentContext, articles: list[RawArticle]) -> None:
        provider = get_provider()
        for start in range(0, len(articles), CLASSIFY_BATCH):
            batch = articles[start : start + CLASSIFY_BATCH]
            payload = [
                {
                    "index": i,
                    "title": a.title,
                    "description": (a.description or "")[:400],
                    "source": a.source_id,
                }
                for i, a in enumerate(batch)
            ]

            try:
                resp = await provider.complete(
                    system=CLASSIFIER_SYSTEM,
                    prompt=json.dumps(payload, ensure_ascii=False),
                    tier="fast",
                    max_tokens=4000,
                    json_schema=CLASSIFIER_SCHEMA,
                )
            except Exception as exc:
                # Classification is enrichment; the ranker degrades gracefully
                # without it rather than failing the run.
                log.warning("classification_failed", error=str(exc))
                continue

            ctx.add_usage(resp.usage)
            await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)

            for entry in (resp.parsed or {}).get("items", []):
                idx = entry.get("index")
                if not isinstance(idx, int) or idx >= len(batch):
                    continue
                article = batch[idx]
                article.categories = entry.get("categories", article.categories)
                article.quality_score = float(entry.get("quality", 0.5))
                article.raw_payload = {
                    **(article.raw_payload or {}),
                    "relevance": entry.get("relevance"),
                    "is_press_release": entry.get("is_press_release"),
                    "entities": entry.get("entities", []),
                    "one_line": entry.get("one_line"),
                }


def _social_score(popularity: dict) -> float:
    """Normalize wildly different engagement scales onto 0-1.

    Log scaling, because the difference between 50 and 500 HN points is
    meaningful while the difference between 3000 and 3500 is noise.
    """
    import math

    if not popularity:
        return 0.0

    platform = popularity.get("platform")
    ceilings = {
        "hackernews": ("points", 1000),
        "reddit": ("score", 5000),
        "github": ("stars", 10000),
        "producthunt": ("votes", 1500),
    }
    if platform not in ceilings:
        return 0.0

    key, ceiling = ceilings[platform]
    value = float(popularity.get(key, 0))
    if value <= 0:
        return 0.0
    return round(min(1.0, math.log1p(value) / math.log1p(ceiling)), 4)
