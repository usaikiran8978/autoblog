"""Agent 2 — Duplicate Detector.

The same story arrives from 6 sources within an hour. This agent collapses
those into one cluster and picks the best representative, so the writer sees
one story rather than six near-identical ones.

Three-stage funnel, cheapest filter first:

  1. Exact URL hash      — free, catches literal reposts (done at ingest).
  2. Title trigram/Jaccard — free, catches syndicated copies with identical
                            headlines before we pay for any embeddings.
  3. Semantic similarity  — embeddings + cosine, catches the real case: the
                            same story written up independently by six outlets
                            under six different headlines.

Clustering is single-link agglomerative over the similarity matrix (union-find).
At ~500 articles per run the O(n²) matrix is ~1 MB and a few milliseconds, so
the simple algorithm is the right one. Above ~5k articles/run, switch to an
ANN index — the vector store already supports it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select

from app.agents.base import Agent, AgentContext, record_cost
from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import DUPLICATES_DROPPED
from app.db.models import RawArticle, Source
from app.services.embeddings import embed_texts, embedding_text, similarity_matrix

log = get_logger(__name__)

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with",
    "is", "are", "at", "by", "from", "as", "its", "it", "new", "now",
}

# Title overlap above this is treated as a duplicate without embedding it.
TITLE_JACCARD_THRESHOLD = 0.75


@dataclass(slots=True)
class DedupeResult:
    unique_ids: list[str]
    clusters: dict[str, list[str]]
    duplicates_removed: int


class DeduplicatorAgent(Agent[list[str], DedupeResult]):
    name = "deduplicator"
    optional = False

    async def execute(self, ctx: AgentContext, payload: list[str]) -> DedupeResult:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=settings.COLLECT_LOOKBACK_HOURS
        )
        articles = (
            await ctx.db.execute(
                select(RawArticle)
                .where(
                    RawArticle.collected_at >= cutoff,
                    RawArticle.is_duplicate.is_(False),
                )
                .order_by(RawArticle.published_at.desc().nullslast())
            )
        ).scalars().all()

        if len(articles) < 2:
            return DedupeResult([str(a.id) for a in articles], {}, 0)

        log.info("dedupe_started", candidates=len(articles))

        # ---- stage 2: lexical prefilter --------------------------------
        token_sets = [_title_tokens(a.title) for a in articles]
        parent = list(range(len(articles)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)

        for i in range(len(articles)):
            for j in range(i + 1, len(articles)):
                if _jaccard(token_sets[i], token_sets[j]) >= TITLE_JACCARD_THRESHOLD:
                    union(i, j)

        # ---- stage 3: semantic ------------------------------------------
        # Reuse stored embeddings; only embed what we have not seen before.
        to_embed_idx = [
            i for i, a in enumerate(articles)
            if a.embedding is None or a.embedding_model != settings.EMBEDDING_MODEL
        ]
        if to_embed_idx:
            texts = [
                embedding_text(articles[i].title, articles[i].description)
                for i in to_embed_idx
            ]
            vectors, usage = await embed_texts(texts)
            ctx.add_usage(usage)
            await record_cost(ctx.db, "openai", settings.EMBEDDING_MODEL, "embedding", usage)
            for idx, vector in zip(to_embed_idx, vectors, strict=True):
                articles[idx].embedding = vector
                articles[idx].embedding_model = settings.EMBEDDING_MODEL

        vectors_all = [list(a.embedding) for a in articles if a.embedding is not None]
        if len(vectors_all) == len(articles):
            sims = similarity_matrix(vectors_all)
            threshold = settings.DEDUPE_SIMILARITY_THRESHOLD
            # Upper triangle only — sims is symmetric.
            pairs = np.argwhere(np.triu(sims, k=1) >= threshold)
            for i, j in pairs:
                union(int(i), int(j))
        else:
            log.warning("dedupe_semantic_skipped", reason="missing embeddings")

        # ---- resolve clusters ------------------------------------------
        groups: dict[int, list[int]] = {}
        for i in range(len(articles)):
            groups.setdefault(find(i), []).append(i)

        source_trust = await self._source_trust(ctx)
        unique_ids: list[str] = []
        clusters: dict[str, list[str]] = {}
        removed = 0

        for members in groups.values():
            cluster_id = uuid.uuid4()
            best = max(members, key=lambda i: _representative_score(articles[i], source_trust))
            winner = articles[best]
            winner.cluster_id = cluster_id
            unique_ids.append(str(winner.id))

            for i in members:
                articles[i].cluster_id = cluster_id
                if i == best:
                    continue
                articles[i].is_duplicate = True
                articles[i].duplicate_of_id = winner.id
                removed += 1

            clusters[str(winner.id)] = [str(articles[i].id) for i in members]

        DUPLICATES_DROPPED.inc(removed)
        log.info(
            "dedupe_completed",
            input=len(articles),
            unique=len(unique_ids),
            removed=removed,
            compression=round(removed / len(articles), 3),
        )
        return DedupeResult(unique_ids, clusters, removed)

    async def _source_trust(self, ctx: AgentContext) -> dict[int, float]:
        rows = await ctx.db.execute(select(Source.id, Source.trust_score))
        return {row[0]: float(row[1]) for row in rows}


def _title_tokens(title: str) -> set[str]:
    return {t for t in _WORD.findall(title.lower()) if t not in _STOPWORDS and len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _representative_score(article: RawArticle, trust: dict[int, float]) -> float:
    """Pick the cluster's best representative.

    Source authority dominates — when six outlets cover an OpenAI launch, the
    OpenAI blog post is the one the writer should work from. Body text is
    weighted next, because a representative with extracted content gives the
    writer far more to reason about.
    """
    score = trust.get(article.source_id, 0.5) * 3.0
    score += (article.quality_score or 0.5) * 2.0
    score += article.social_score * 1.5
    if article.content:
        score += 1.5
    if article.description:
        score += 0.5
    if article.published_at:
        age_h = (datetime.now(timezone.utc) - article.published_at).total_seconds() / 3600
        score += max(0.0, 1.0 - age_h / 48.0)
    return score
