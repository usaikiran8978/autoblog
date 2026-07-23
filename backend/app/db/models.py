"""SQLAlchemy 2.0 models — the full persistence layer.

Design notes
------------
* `raw_articles` is append-only ingest. `posts` is the published artifact.
  Keeping them separate means a re-run can re-rank the same corpus without
  touching published history.
* Embeddings live in a `vector` column (pgvector) with an HNSW index so
  dedupe is a single indexed query rather than an N^2 Python loop.
* `url_hash` (unique) is the ingest idempotency key — re-running collection
  is a no-op instead of a duplicate storm.
* `agent_runs` records tokens and cost per agent invocation. That table is
  what makes "what did last Tuesday's 6 PM post cost?" a one-line query.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB, list: JSONB}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------- enums
class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    partial = "partial"
    cancelled = "cancelled"


class PostStatus(str, enum.Enum):
    draft = "draft"
    ready_for_review = "ready_for_review"
    approved = "approved"
    publishing = "publishing"
    published = "published"
    failed = "failed"
    rejected = "rejected"


class PublicationStatus(str, enum.Enum):
    pending = "pending"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class SourceKind(str, enum.Enum):
    rss = "rss"
    api = "api"
    scrape = "scrape"


# ------------------------------------------------------------------- sources
class Source(Base, TimestampMixin):
    """Registry of every place we pull from. Seeded from
    `app/services/source_registry.py` but editable at runtime via the API,
    so adding a feed does not require a deploy."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind, name="source_kind"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str | None] = mapped_column(Text)

    categories: Mapped[list] = mapped_column(JSONB, default=list)
    # 0.0-1.0 editorial trust. Feeds the ranker; a first-party vendor blog
    # outranks an aggregator repost of the same story.
    trust_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Politeness + conditional-GET bookkeeping.
    fetch_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=30)
    etag: Mapped[str | None] = mapped_column(String(255))
    last_modified: Mapped[str | None] = mapped_column(String(255))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    config: Mapped[dict] = mapped_column(JSONB, default=dict)

    articles: Mapped[list["RawArticle"]] = relationship(back_populates="source")

    __table_args__ = (
        CheckConstraint("trust_score >= 0 AND trust_score <= 1", name="ck_trust_range"),
        Index("ix_sources_enabled_kind", "enabled", "kind"),
    )


# -------------------------------------------------------------- raw articles
class RawArticle(Base, TimestampMixin):
    """One item as collected. Never edited after ingest."""

    __tablename__ = "raw_articles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256(normalized_url) — the ingest idempotency key.
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)

    description: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)  # extracted body, if scraped
    author: Mapped[str | None] = mapped_column(String(255))
    image_url: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8), default="en")

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    categories: Mapped[list] = mapped_column(JSONB, default=list)
    tags: Mapped[list] = mapped_column(JSONB, default=list)

    # Engagement signals, normalized across sources (HN points, Reddit ups,
    # GitHub stars, PH votes). `social_score` is the blended 0-1 value.
    popularity_raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    social_score: Mapped[float] = mapped_column(Float, default=0.0)

    quality_score: Mapped[float | None] = mapped_column(Float)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_articles.id", ondelete="SET NULL")
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))

    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    source: Mapped[Source] = relationship(back_populates="articles")

    __table_args__ = (
        Index("ix_raw_articles_published", "published_at"),
        Index("ix_raw_articles_collected", "collected_at"),
        Index("ix_raw_articles_dupe", "is_duplicate", "published_at"),
        Index(
            "ix_raw_articles_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Trigram index for cheap lexical near-dupe prefilter before we pay
        # for embeddings at all.
        Index(
            "ix_raw_articles_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )


# ------------------------------------------------------------ pipeline runs
class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger: Mapped[str] = mapped_column(String(32), default="schedule")  # schedule|manual|retry
    slot: Mapped[str | None] = mapped_column(String(16))  # "morning" | "evening"
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.pending, index=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    articles_collected: Mapped[int] = mapped_column(Integer, default=0)
    articles_after_dedupe: Mapped[int] = mapped_column(Integer, default=0)
    articles_ranked: Mapped[int] = mapped_column(Integer, default=0)
    posts_created: Mapped[int] = mapped_column(Integer, default=0)

    total_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), default=0)

    error: Mapped[str | None] = mapped_column(Text)
    stage_timings: Mapped[dict] = mapped_column(JSONB, default=dict)

    posts: Mapped[list["Post"]] = relationship(back_populates="run")
    agent_runs: Mapped[list["AgentRun"]] = relationship(back_populates="run")

    __table_args__ = (Index("ix_runs_created_status", "created_at", "status"),)


class AgentRun(Base, TimestampMixin):
    """One agent invocation. The unit of cost accounting."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), index=True
    )
    agent: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, name="run_status"))

    provider: Mapped[str | None] = mapped_column(String(24))
    model: Mapped[str | None] = mapped_column(String(80))

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), default=0)

    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    run: Mapped[PipelineRun] = relationship(back_populates="agent_runs")


# --------------------------------------------------------------------- post
class Post(Base, TimestampMixin):
    """The generated article and everything hanging off it."""

    __tablename__ = "posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)

    executive_summary: Mapped[str | None] = mapped_column(Text)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)

    highlights: Mapped[list] = mapped_column(JSONB, default=list)
    key_takeaways: Mapped[list] = mapped_column(JSONB, default=list)
    expert_opinion: Mapped[str | None] = mapped_column(Text)
    industry_impact: Mapped[str | None] = mapped_column(Text)
    future_predictions: Mapped[str | None] = mapped_column(Text)

    word_count: Mapped[int] = mapped_column(Integer, default=0)
    reading_time_minutes: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str | None] = mapped_column(String(80), index=True)

    status: Mapped[PostStatus] = mapped_column(
        Enum(PostStatus, name="post_status"), default=PostStatus.draft, index=True
    )

    # Provenance: which raw_articles fed this post, with attribution links.
    source_article_ids: Mapped[list] = mapped_column(JSONB, default=list)
    citations: Mapped[list] = mapped_column(JSONB, default=list)

    # Guardrail scores from the QA pass.
    originality_score: Mapped[float | None] = mapped_column(Float)
    max_source_similarity: Mapped[float | None] = mapped_column(Float)
    quality_notes: Mapped[dict] = mapped_column(JSONB, default=dict)

    provider: Mapped[str | None] = mapped_column(String(24))
    model: Mapped[str | None] = mapped_column(String(80))
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), default=0)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[PipelineRun | None] = relationship(back_populates="posts")
    seo: Mapped["PostSEO | None"] = relationship(
        back_populates="post", uselist=False, cascade="all, delete-orphan"
    )
    images: Mapped[list["PostImage"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    social_posts: Mapped[list["SocialPost"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    publications: Mapped[list["Publication"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_posts_status_created", "status", "created_at"),)


class PostSEO(Base, TimestampMixin):
    __tablename__ = "post_seo"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), unique=True
    )

    seo_title: Mapped[str] = mapped_column(String(70), nullable=False)
    meta_description: Mapped[str] = mapped_column(String(165), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    focus_keyword: Mapped[str | None] = mapped_column(String(120))
    keywords: Mapped[list] = mapped_column(JSONB, default=list)

    json_ld: Mapped[dict] = mapped_column(JSONB, default=dict)   # schema.org
    og_tags: Mapped[dict] = mapped_column(JSONB, default=dict)   # Open Graph
    twitter_card: Mapped[dict] = mapped_column(JSONB, default=dict)
    faq: Mapped[list] = mapped_column(JSONB, default=list)       # [{question, answer}]

    readability_score: Mapped[float | None] = mapped_column(Float)

    post: Mapped[Post] = relationship(back_populates="seo")


class PostImage(Base, TimestampMixin):
    __tablename__ = "post_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))

    role: Mapped[str] = mapped_column(String(24), default="featured")  # featured|inline|og
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str | None] = mapped_column(String(80))

    storage_path: Mapped[str | None] = mapped_column(Text)
    public_url: Mapped[str | None] = mapped_column(Text)
    remote_media_id: Mapped[str | None] = mapped_column(String(80))  # CMS media ID
    alt_text: Mapped[str | None] = mapped_column(Text)

    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), default=0)

    post: Mapped[Post] = relationship(back_populates="images")


class SocialPost(Base, TimestampMixin):
    __tablename__ = "social_posts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))

    platform: Mapped[str] = mapped_column(String(24))  # linkedin|twitter|facebook|threads|instagram
    body: Mapped[str] = mapped_column(Text, nullable=False)
    hashtags: Mapped[list] = mapped_column(JSONB, default=list)
    cta: Mapped[str | None] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(120))

    post: Mapped[Post] = relationship(back_populates="social_posts")

    __table_args__ = (UniqueConstraint("post_id", "platform", name="uq_social_post_platform"),)


class Publication(Base, TimestampMixin):
    """One row per (post, target). Retried independently — a WordPress
    failure must not roll back a successful Ghost publish."""

    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))

    target: Mapped[str] = mapped_column(String(24))
    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus, name="publication_status"), default=PublicationStatus.pending
    )
    external_id: Mapped[str | None] = mapped_column(String(120))
    external_url: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    response_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    post: Mapped[Post] = relationship(back_populates="publications")

    __table_args__ = (UniqueConstraint("post_id", "target", name="uq_publication_target"),)


class AnalyticsSnapshot(Base, TimestampMixin):
    """Periodic traffic/engagement pull per post. Append-only time series."""

    __tablename__ = "analytics_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    source: Mapped[str] = mapped_column(String(32), default="internal")

    pageviews: Mapped[int] = mapped_column(Integer, default=0)
    unique_visitors: Mapped[int] = mapped_column(Integer, default=0)
    avg_time_on_page_s: Mapped[float] = mapped_column(Float, default=0)
    bounce_rate: Mapped[float | None] = mapped_column(Float)
    social_shares: Mapped[int] = mapped_column(Integer, default=0)
    backlinks: Mapped[int] = mapped_column(Integer, default=0)
    search_impressions: Mapped[int] = mapped_column(Integer, default=0)
    search_clicks: Mapped[int] = mapped_column(Integer, default=0)
    avg_position: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class CostLedger(Base, TimestampMixin):
    """Daily rollup used by the budget guard. Cheap to query on every LLM call."""

    __tablename__ = "cost_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(24))  # llm|embedding|image

    requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), default=0)

    __table_args__ = (
        UniqueConstraint("day", "provider", "model", "category", name="uq_cost_day_model"),
    )


class IdempotencyKey(Base):
    """Guards against a double-fired scheduler creating two posts for one slot."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
