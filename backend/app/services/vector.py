"""Vector search abstraction.

`pgvector` is the default: one datastore, transactional consistency with the
articles themselves, and an HNSW index that comfortably handles low millions
of rows. `qdrant` is the swap-in for when the corpus outgrows that — the
interface is identical so nothing above this module changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging_conf import get_logger
from app.db.models import RawArticle

log = get_logger(__name__)


@dataclass(slots=True)
class Neighbor:
    article_id: uuid.UUID
    title: str
    url: str
    similarity: float


class PgVectorStore:
    """Cosine-distance search against `raw_articles.embedding`.

    pgvector's `<=>` is cosine *distance*, so similarity = 1 - distance.
    """

    async def search(
        self,
        db: AsyncSession,
        embedding: list[float],
        *,
        limit: int = 10,
        min_similarity: float = 0.7,
        exclude_id: uuid.UUID | None = None,
        within_days: int = 14,
    ) -> list[Neighbor]:
        max_distance = 1.0 - min_similarity
        sql = text(
            """
            SELECT id, title, url, 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
            FROM raw_articles
            WHERE embedding IS NOT NULL
              AND collected_at > NOW() - CAST(:window AS interval)
              AND (:exclude IS NULL OR id <> CAST(:exclude AS uuid))
              AND (embedding <=> CAST(:emb AS vector)) < :max_distance
            ORDER BY embedding <=> CAST(:emb AS vector)
            LIMIT :limit
            """
        )
        rows = await db.execute(
            sql,
            {
                "emb": str(embedding),
                "window": f"{within_days} days",
                "exclude": str(exclude_id) if exclude_id else None,
                "max_distance": max_distance,
                "limit": limit,
            },
        )
        return [
            Neighbor(article_id=r.id, title=r.title, url=r.url, similarity=float(r.similarity))
            for r in rows
        ]

    async def upsert(self, db: AsyncSession, article_id: uuid.UUID, embedding: list[float]) -> None:
        article = await db.get(RawArticle, article_id)
        if article:
            article.embedding = embedding
            article.embedding_model = settings.EMBEDDING_MODEL


class QdrantStore:
    """Drop-in alternative for large corpora / cross-region replication."""

    def __init__(self) -> None:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import Distance, VectorParams

        self._models = __import__("qdrant_client.models", fromlist=["models"])
        self.client = AsyncQdrantClient(
            url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY
        )
        self.collection = settings.QDRANT_COLLECTION
        self._vector_params = VectorParams(
            size=settings.EMBEDDING_DIM, distance=Distance.COSINE
        )

    async def ensure_collection(self) -> None:
        existing = {c.name for c in (await self.client.get_collections()).collections}
        if self.collection not in existing:
            await self.client.create_collection(
                collection_name=self.collection, vectors_config=self._vector_params
            )
            log.info("qdrant_collection_created", collection=self.collection)

    async def search(
        self,
        db: AsyncSession,
        embedding: list[float],
        *,
        limit: int = 10,
        min_similarity: float = 0.7,
        exclude_id: uuid.UUID | None = None,
        within_days: int = 14,
    ) -> list[Neighbor]:
        hits = await self.client.search(
            collection_name=self.collection,
            query_vector=embedding,
            limit=limit + 1,
            score_threshold=min_similarity,
        )
        return [
            Neighbor(
                article_id=uuid.UUID(str(h.id)),
                title=(h.payload or {}).get("title", ""),
                url=(h.payload or {}).get("url", ""),
                similarity=float(h.score),
            )
            for h in hits
            if str(h.id) != str(exclude_id)
        ][:limit]

    async def upsert(self, db: AsyncSession, article_id: uuid.UUID, embedding: list[float]) -> None:
        article = await db.get(RawArticle, article_id)
        payload = {"title": article.title, "url": article.url} if article else {}
        await self.client.upsert(
            collection_name=self.collection,
            points=[
                self._models.PointStruct(id=str(article_id), vector=embedding, payload=payload)
            ],
        )


_store: PgVectorStore | QdrantStore | None = None


def get_vector_store() -> PgVectorStore | QdrantStore:
    global _store
    if _store is None:
        _store = QdrantStore() if settings.VECTOR_BACKEND == "qdrant" else PgVectorStore()
    return _store


async def recent_published_titles(db: AsyncSession, days: int = 30) -> list[str]:
    """Titles we already published — used to stop the pipeline covering the
    same story twice in a week."""
    from app.db.models import Post, PostStatus

    rows = await db.execute(
        select(Post.title).where(
            Post.status == PostStatus.published,
            Post.published_at > text(f"NOW() - INTERVAL '{days} days'"),
        )
    )
    return [r[0] for r in rows]
