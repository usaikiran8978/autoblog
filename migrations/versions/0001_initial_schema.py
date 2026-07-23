"""Initial schema.

Creates the required Postgres extensions before any table, because both the
vector column and the trigram index depend on them:

  * vector   — pgvector, for embedding storage + HNSW similarity search
  * pg_trgm  — trigram index on titles, the cheap lexical dedupe prefilter
  * unaccent — normalizes accented characters in title matching

Revision ID: 0001
"""

from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1536  # keep in sync with settings.EMBEDDING_DIM


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # `create_type=False` is load-bearing. Without it SQLAlchemy emits a
    # CREATE TYPE again for every column that references the enum, so the
    # first create_table fails with "type already exists" — after the explicit
    # creation below has already succeeded. That leaves the database holding
    # the types but no tables, and no alembic_version row to record progress.
    #
    # With create_type=False the explicit create(checkfirst=True) is the only
    # thing that emits DDL, which also makes this migration re-runnable
    # against a database left in that partial state.
    enum_kwargs = {"create_type": False}

    source_kind = postgresql.ENUM("rss", "api", "scrape", name="source_kind", **enum_kwargs)
    run_status = postgresql.ENUM(
        "pending", "running", "succeeded", "failed", "partial", "cancelled",
        name="run_status", **enum_kwargs,
    )
    post_status = postgresql.ENUM(
        "draft", "ready_for_review", "approved", "publishing", "published",
        "failed", "rejected", name="post_status", **enum_kwargs,
    )
    publication_status = postgresql.ENUM(
        "pending", "success", "failed", "skipped", name="publication_status", **enum_kwargs
    )
    for enum in (source_kind, run_status, post_status, publication_status):
        enum.create(op.get_bind(), checkfirst=True)

    # ---------------------------------------------------------- sources
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("kind", source_kind, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("homepage", sa.Text),
        sa.Column("categories", postgresql.JSONB, server_default="[]"),
        sa.Column("trust_score", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("fetch_interval_minutes", sa.Integer, server_default="60"),
        sa.Column("rate_limit_per_min", sa.Integer, server_default="30"),
        sa.Column("etag", sa.String(255)),
        sa.Column("last_modified", sa.String(255)),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("consecutive_failures", sa.Integer, server_default="0"),
        sa.Column("config", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("trust_score >= 0 AND trust_score <= 1", name="ck_trust_range"),
    )
    op.create_index("ix_sources_enabled_kind", "sources", ["enabled", "kind"])

    # ----------------------------------------------------- raw_articles
    op.create_table(
        "raw_articles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", sa.Integer,
                  sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("canonical_url", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("content", sa.Text),
        sa.Column("author", sa.String(255)),
        sa.Column("image_url", sa.Text),
        sa.Column("language", sa.String(8), server_default="en"),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("categories", postgresql.JSONB, server_default="[]"),
        sa.Column("tags", postgresql.JSONB, server_default="[]"),
        sa.Column("popularity_raw", postgresql.JSONB, server_default="{}"),
        sa.Column("social_score", sa.Float, server_default="0"),
        sa.Column("quality_score", sa.Float),
        sa.Column("is_duplicate", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("duplicate_of_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("raw_articles.id", ondelete="SET NULL")),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True)),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM)),
        sa.Column("embedding_model", sa.String(80)),
        sa.Column("raw_payload", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_raw_articles_published", "raw_articles", ["published_at"])
    op.create_index("ix_raw_articles_collected", "raw_articles", ["collected_at"])
    op.create_index("ix_raw_articles_dupe", "raw_articles", ["is_duplicate", "published_at"])
    op.create_index("ix_raw_articles_cluster_id", "raw_articles", ["cluster_id"])
    op.execute(
        "CREATE INDEX ix_raw_articles_embedding_hnsw ON raw_articles "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX ix_raw_articles_title_trgm ON raw_articles "
        "USING gin (title gin_trgm_ops)"
    )

    # ---------------------------------------------------- pipeline_runs
    op.create_table(
        "pipeline_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("trigger", sa.String(32), server_default="schedule"),
        sa.Column("slot", sa.String(16)),
        sa.Column("status", run_status, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("articles_collected", sa.Integer, server_default="0"),
        sa.Column("articles_after_dedupe", sa.Integer, server_default="0"),
        sa.Column("articles_ranked", sa.Integer, server_default="0"),
        sa.Column("posts_created", sa.Integer, server_default="0"),
        sa.Column("total_input_tokens", sa.BigInteger, server_default="0"),
        sa.Column("total_output_tokens", sa.BigInteger, server_default="0"),
        sa.Column("total_cost_usd", sa.Numeric(10, 5), server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("stage_timings", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index("ix_runs_created_status", "pipeline_runs", ["created_at", "status"])

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent", sa.String(48), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("provider", sa.String(24)),
        sa.Column("model", sa.String(80)),
        sa.Column("input_tokens", sa.Integer, server_default="0"),
        sa.Column("output_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 5), server_default="0"),
        sa.Column("duration_ms", sa.Integer, server_default="0"),
        sa.Column("attempts", sa.Integer, server_default="1"),
        sa.Column("error", sa.Text),
        sa.Column("meta", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_run_id", "agent_runs", ["run_id"])
    op.create_index("ix_agent_runs_agent", "agent_runs", ["agent"])

    # ------------------------------------------------------------ posts
    op.create_table(
        "posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("subtitle", sa.Text),
        sa.Column("slug", sa.String(220), nullable=False, unique=True),
        sa.Column("executive_summary", sa.Text),
        sa.Column("body_markdown", sa.Text, nullable=False),
        sa.Column("body_html", sa.Text),
        sa.Column("highlights", postgresql.JSONB, server_default="[]"),
        sa.Column("key_takeaways", postgresql.JSONB, server_default="[]"),
        sa.Column("expert_opinion", sa.Text),
        sa.Column("industry_impact", sa.Text),
        sa.Column("future_predictions", sa.Text),
        sa.Column("word_count", sa.Integer, server_default="0"),
        sa.Column("reading_time_minutes", sa.Integer, server_default="0"),
        sa.Column("category", sa.String(80)),
        sa.Column("status", post_status, server_default="draft"),
        sa.Column("source_article_ids", postgresql.JSONB, server_default="[]"),
        sa.Column("citations", postgresql.JSONB, server_default="[]"),
        sa.Column("originality_score", sa.Float),
        sa.Column("max_source_similarity", sa.Float),
        sa.Column("quality_notes", postgresql.JSONB, server_default="{}"),
        sa.Column("provider", sa.String(24)),
        sa.Column("model", sa.String(80)),
        sa.Column("cost_usd", sa.Numeric(10, 5), server_default="0"),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_posts_run_id", "posts", ["run_id"])
    op.create_index("ix_posts_status", "posts", ["status"])
    op.create_index("ix_posts_category", "posts", ["category"])
    op.create_index("ix_posts_status_created", "posts", ["status", "created_at"])

    op.create_table(
        "post_seo",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("post_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("seo_title", sa.String(70), nullable=False),
        sa.Column("meta_description", sa.String(165), nullable=False),
        sa.Column("canonical_url", sa.Text),
        sa.Column("focus_keyword", sa.String(120)),
        sa.Column("keywords", postgresql.JSONB, server_default="[]"),
        sa.Column("json_ld", postgresql.JSONB, server_default="{}"),
        sa.Column("og_tags", postgresql.JSONB, server_default="{}"),
        sa.Column("twitter_card", postgresql.JSONB, server_default="{}"),
        sa.Column("faq", postgresql.JSONB, server_default="[]"),
        sa.Column("readability_score", sa.Float),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "post_images",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("post_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(24), server_default="featured"),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("negative_prompt", sa.Text),
        sa.Column("provider", sa.String(24)),
        sa.Column("model", sa.String(80)),
        sa.Column("storage_path", sa.Text),
        sa.Column("public_url", sa.Text),
        sa.Column("remote_media_id", sa.String(80)),
        sa.Column("alt_text", sa.Text),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("bytes", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 5), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "social_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("post_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("hashtags", postgresql.JSONB, server_default="[]"),
        sa.Column("cta", sa.Text),
        sa.Column("char_count", sa.Integer, server_default="0"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("external_id", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("post_id", "platform", name="uq_social_post_platform"),
    )

    op.create_table(
        "publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("post_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target", sa.String(24), nullable=False),
        sa.Column("status", publication_status, server_default="pending"),
        sa.Column("external_id", sa.String(120)),
        sa.Column("external_url", sa.Text),
        sa.Column("attempts", sa.Integer, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("response_payload", postgresql.JSONB, server_default="{}"),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("post_id", "target", name="uq_publication_target"),
    )

    # -------------------------------------------------------- analytics
    op.create_table(
        "analytics_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("post_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("source", sa.String(32), server_default="internal"),
        sa.Column("pageviews", sa.Integer, server_default="0"),
        sa.Column("unique_visitors", sa.Integer, server_default="0"),
        sa.Column("avg_time_on_page_s", sa.Float, server_default="0"),
        sa.Column("bounce_rate", sa.Float),
        sa.Column("social_shares", sa.Integer, server_default="0"),
        sa.Column("backlinks", sa.Integer, server_default="0"),
        sa.Column("search_impressions", sa.Integer, server_default="0"),
        sa.Column("search_clicks", sa.Integer, server_default="0"),
        sa.Column("avg_position", sa.Float),
        sa.Column("raw", postgresql.JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_post_id", "analytics_snapshots", ["post_id"])
    op.create_index("ix_analytics_captured_at", "analytics_snapshots", ["captured_at"])

    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("day", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("requests", sa.Integer, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 5), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("day", "provider", "model", "category", name="uq_cost_day_model"),
    )
    op.create_index("ix_cost_ledger_day", "cost_ledger", ["day"])

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(160), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON, server_default="{}"),
    )
    op.create_index("ix_idempotency_expires", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    for table in (
        "idempotency_keys", "cost_ledger", "analytics_snapshots", "publications",
        "social_posts", "post_images", "post_seo", "posts", "agent_runs",
        "pipeline_runs", "raw_articles", "sources",
    ):
        op.drop_table(table)

    for enum in ("publication_status", "post_status", "run_status", "source_kind"):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
