"""Prometheus metrics. Scraped at GET /metrics; dashboards in docs/BLUEPRINT.md §Monitoring."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ------------------------------------------------------------------ pipeline
PIPELINE_RUNS = Counter(
    "autoblog_pipeline_runs_total", "Pipeline runs", ["trigger", "status"]
)
PIPELINE_DURATION = Histogram(
    "autoblog_pipeline_duration_seconds",
    "End-to-end pipeline duration",
    buckets=(30, 60, 120, 300, 600, 900, 1800, 3600),
)
STAGE_DURATION = Histogram(
    "autoblog_stage_duration_seconds",
    "Per-agent stage duration",
    ["agent"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)
STAGE_FAILURES = Counter(
    "autoblog_stage_failures_total", "Agent stage failures", ["agent", "error"]
)

# ---------------------------------------------------------------- collection
ARTICLES_COLLECTED = Counter(
    "autoblog_articles_collected_total", "Raw articles ingested", ["source"]
)
SOURCE_ERRORS = Counter(
    "autoblog_source_errors_total", "Source fetch errors", ["source", "kind"]
)
DUPLICATES_DROPPED = Counter(
    "autoblog_duplicates_dropped_total", "Articles dropped as near-duplicates"
)

# ----------------------------------------------------------------------- llm
LLM_REQUESTS = Counter(
    "autoblog_llm_requests_total", "LLM calls", ["provider", "model", "status"]
)
LLM_TOKENS = Counter(
    "autoblog_llm_tokens_total", "Token usage", ["provider", "model", "kind"]
)
LLM_LATENCY = Histogram(
    "autoblog_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)
LLM_COST = Counter(
    "autoblog_llm_cost_usd_total", "Estimated spend in USD", ["provider", "model"]
)
DAILY_SPEND = Gauge("autoblog_daily_spend_usd", "Rolling 24h estimated spend")

# ---------------------------------------------------------------- publishing
POSTS_PUBLISHED = Counter(
    "autoblog_posts_published_total", "Posts published", ["target", "status"]
)
IMAGES_GENERATED = Counter(
    "autoblog_images_generated_total", "Featured images generated", ["provider", "status"]
)
SOCIAL_POSTS = Counter(
    "autoblog_social_posts_total", "Social variants generated", ["platform"]
)
QUEUE_DEPTH = Gauge("autoblog_queue_depth", "Celery queue depth", ["queue"])
