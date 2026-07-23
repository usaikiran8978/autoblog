"""Embedding generation with aggressive reuse.

Embeddings are the single most re-computable cost in the pipeline: the same
article shows up across runs, and the same headline shows up across sources.
Two layers of reuse:

  1. Redis content-hash cache (30 days) — same text, same model => never pay twice.
  2. DB reuse — an article already carrying an embedding for the current model
     is skipped entirely.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.resilience import redis_client
from app.llm.base import Usage
from app.llm.factory import get_embedding_provider

log = get_logger(__name__)

CACHE_TTL_SECONDS = 30 * 24 * 3600
_CACHE_PREFIX = "emb:v1"


def _key(text: str, model: str) -> str:
    digest = hashlib.sha256(f"{model}:{text}".encode()).hexdigest()
    return f"{_CACHE_PREFIX}:{digest}"


def embedding_text(title: str, description: str | None = None) -> str:
    """Canonical text for a story. Title carries most of the dedupe signal;
    a bounded slice of description disambiguates same-headline stories."""
    parts = [title.strip()]
    if description:
        parts.append(description.strip()[:500])
    return " \n".join(parts)


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], Usage]:
    """Embed with cache-through. Returns vectors in input order."""
    if not texts:
        return [], Usage()

    model = settings.EMBEDDING_MODEL
    r = redis_client()

    keys = [_key(t, model) for t in texts]
    cached_raw = await r.mget(keys)

    results: list[list[float] | None] = [None] * len(texts)
    misses: list[int] = []

    for i, blob in enumerate(cached_raw):
        if blob:
            try:
                results[i] = json.loads(blob)
                continue
            except json.JSONDecodeError:
                pass
        misses.append(i)

    usage = Usage()
    if misses:
        provider = get_embedding_provider()
        vectors, usage = await provider.embed([texts[i] for i in misses])

        pipe = r.pipeline()
        for idx, vector in zip(misses, vectors, strict=True):
            results[idx] = vector
            pipe.setex(keys[idx], CACHE_TTL_SECONDS, json.dumps(vector))
        await pipe.execute()

    hit_rate = (len(texts) - len(misses)) / len(texts)
    log.info(
        "embeddings_generated",
        total=len(texts),
        cache_hits=len(texts) - len(misses),
        hit_rate=round(hit_rate, 3),
        cost_usd=round(usage.cost_usd, 5),
    )
    return [v for v in results if v is not None], usage


def cosine_similarity(a: list[float] | np.ndarray, b: list[float] | np.ndarray) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return 0.0 if denom == 0 else float(np.dot(va, vb) / denom)


def similarity_matrix(vectors: list[list[float]]) -> np.ndarray:
    """All-pairs cosine similarity via one normalized matmul.

    O(n²) in memory but n is ~500 per run, so this is a 1 MB array and a few
    milliseconds — far cheaper than 125k individual cosine calls in Python.
    """
    if not vectors:
        return np.zeros((0, 0), dtype=np.float32)
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = matrix / norms
    return normalized @ normalized.T
