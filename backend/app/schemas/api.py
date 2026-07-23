"""Pydantic request/response models. These define the public API contract."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------- runs
class TriggerRunRequest(BaseModel):
    trigger: Literal["manual", "schedule", "retry"] = "manual"
    posts: int | None = Field(default=None, ge=1, le=5,
                              description="Override POSTS_PER_RUN for this run")
    dry_run: bool = Field(default=False,
                          description="Collect, dedupe and rank, but do not write or publish")


class RunSummary(ORMModel):
    id: uuid.UUID
    trigger: str
    slot: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    articles_collected: int
    articles_after_dedupe: int
    articles_ranked: int
    posts_created: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    error: str | None
    stage_timings: dict[str, Any] = {}


class RunAccepted(BaseModel):
    task_id: str
    status: str = "queued"
    message: str


# ------------------------------------------------------------------ posts
class SEOOut(ORMModel):
    seo_title: str
    meta_description: str
    canonical_url: str | None
    focus_keyword: str | None
    keywords: list[str]
    json_ld: dict
    og_tags: dict
    twitter_card: dict
    faq: list[dict]


class ImageOut(ORMModel):
    role: str
    public_url: str | None
    alt_text: str | None
    provider: str
    prompt: str
    width: int | None
    height: int | None


class SocialOut(ORMModel):
    platform: str
    body: str
    hashtags: list[str]
    cta: str | None
    char_count: int


class PublicationOut(ORMModel):
    target: str
    status: str
    external_url: str | None
    attempts: int
    last_error: str | None
    published_at: datetime | None


class PostSummary(ORMModel):
    id: uuid.UUID
    title: str
    subtitle: str | None
    slug: str
    status: str
    category: str | None
    word_count: int
    reading_time_minutes: int
    originality_score: float | None
    cost_usd: float
    created_at: datetime
    published_at: datetime | None
    # Flattened from post_images so a list view can render hero images without
    # the client fetching every post individually.
    featured_image: str | None = None
    featured_image_alt: str | None = None


class PostDetail(PostSummary):
    executive_summary: str | None
    body_markdown: str
    highlights: list[str]
    key_takeaways: list[str]
    expert_opinion: str | None
    industry_impact: str | None
    future_predictions: str | None
    citations: list[dict]
    quality_notes: dict
    max_source_similarity: float | None
    provider: str | None
    model: str | None
    seo: SEOOut | None = None
    images: list[ImageOut] = []
    social_posts: list[SocialOut] = []
    publications: list[PublicationOut] = []


class PostStatusUpdate(BaseModel):
    status: Literal["approved", "rejected", "draft"]
    note: str | None = None


# ---------------------------------------------------------------- sources
class SourceOut(ORMModel):
    id: int
    slug: str
    name: str
    kind: str
    url: str
    categories: list[str]
    trust_score: float
    enabled: bool
    last_fetched_at: datetime | None
    last_error: str | None
    consecutive_failures: int


class SourceCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=2, max_length=160)
    kind: Literal["rss", "api", "scrape"] = "rss"
    url: HttpUrl
    categories: list[str] = []
    trust_score: float = Field(default=0.5, ge=0.0, le=1.0)
    enabled: bool = True
    config: dict = {}


class SourceUpdate(BaseModel):
    name: str | None = None
    url: HttpUrl | None = None
    categories: list[str] | None = None
    trust_score: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool | None = None
    config: dict | None = None


# -------------------------------------------------------------- analytics
class CostBreakdown(BaseModel):
    provider: str
    model: str
    category: str
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CostReport(BaseModel):
    period_days: int
    total_cost_usd: float
    cost_per_post_usd: float
    posts_published: int
    projected_monthly_usd: float
    budget_limit_usd: float
    breakdown: list[CostBreakdown]


class PipelineStats(BaseModel):
    runs_total: int
    runs_succeeded: int
    runs_failed: int
    success_rate: float
    avg_duration_seconds: float
    avg_cost_per_run_usd: float
    posts_published: int
    articles_collected: int
    dedupe_compression: float


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    environment: str
    checks: dict[str, Any] = {}
