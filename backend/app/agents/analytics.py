"""Agent 9 — Analytics Agent.

Two jobs:
  * At the end of a run, snapshot what the run produced and cost.
  * On a separate daily schedule, pull traffic metrics for published posts and
    append time-series rows to `analytics_snapshots`.

The feedback loop this enables is the point: after ~30 posts you can correlate
category, source, publish slot and word count against pageviews, and feed that
back into the ranker's weights. That is the difference between a content
firehose and a system that gets better.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.agents.base import Agent, AgentContext
from app.core.logging_conf import get_logger
from app.core.resilience import http_client
from app.db.models import (
    AgentRun,
    AnalyticsSnapshot,
    PipelineRun,
    Post,
    PostStatus,
    Publication,
    PublicationStatus,
)

log = get_logger(__name__)


@dataclass(slots=True)
class RunAnalytics:
    run_id: str
    posts_created: int
    total_cost_usd: float
    cost_per_post: float
    duration_seconds: float
    stage_timings: dict


class AnalyticsAgent(Agent[None, RunAnalytics]):
    name = "analytics"
    optional = True

    async def execute(self, ctx: AgentContext, payload: None = None) -> RunAnalytics:
        run = await ctx.db.get(PipelineRun, ctx.run_id)
        if not run:
            raise ValueError(f"run {ctx.run_id} not found")

        agent_rows = (
            await ctx.db.execute(select(AgentRun).where(AgentRun.run_id == ctx.run_id))
        ).scalars().all()

        posts = (
            await ctx.db.scalar(
                select(func.count(Post.id)).where(Post.run_id == ctx.run_id)
            )
        ) or 0

        total_cost = sum(float(r.cost_usd) for r in agent_rows)
        duration = (
            (run.finished_at or datetime.now(timezone.utc)) - (run.started_at or run.created_at)
        ).total_seconds()

        run.articles_collected = ctx.state.get("collected", run.articles_collected)
        run.articles_after_dedupe = ctx.state.get("after_dedupe", run.articles_after_dedupe)
        run.articles_ranked = ctx.state.get("ranked", run.articles_ranked)
        run.posts_created = posts
        run.total_cost_usd = total_cost
        run.total_input_tokens = sum(r.input_tokens for r in agent_rows)
        run.total_output_tokens = sum(r.output_tokens for r in agent_rows)
        run.stage_timings = {r.agent: r.duration_ms for r in agent_rows}

        result = RunAnalytics(
            run_id=str(ctx.run_id),
            posts_created=posts,
            total_cost_usd=round(total_cost, 4),
            cost_per_post=round(total_cost / posts, 4) if posts else 0.0,
            duration_seconds=round(duration, 1),
            stage_timings=run.stage_timings,
        )
        log.info("run_analytics", **result.__dict__)
        return result


async def collect_post_metrics(db) -> int:
    """Daily traffic pull for posts published in the last 90 days.

    Ships with a Plausible adapter because it needs one env var and no OAuth
    dance. GA4 works the same way via the Data API — swap `_fetch_plausible`
    for a `_fetch_ga4` and the rest of the pipeline is unchanged.
    """
    site_id = os.getenv("PLAUSIBLE_SITE_ID")
    api_key = os.getenv("PLAUSIBLE_API_KEY")
    if not (site_id and api_key):
        log.info("analytics_pull_skipped", reason="PLAUSIBLE_* not configured")
        return 0

    since = datetime.now(timezone.utc) - timedelta(days=90)
    posts = (
        await db.execute(
            select(Post, Publication.external_url)
            .join(Publication, Publication.post_id == Post.id)
            .where(
                Post.status == PostStatus.published,
                Post.published_at >= since,
                Publication.status == PublicationStatus.success,
            )
        )
    ).all()

    written = 0
    async with http_client() as client:
        for post, url in posts:
            try:
                metrics = await _fetch_plausible(client, site_id, api_key, post.slug)
            except Exception as exc:
                log.warning("analytics_fetch_failed", post_id=str(post.id), error=str(exc))
                continue

            db.add(
                AnalyticsSnapshot(
                    post_id=post.id,
                    source="plausible",
                    pageviews=metrics.get("pageviews", 0),
                    unique_visitors=metrics.get("visitors", 0),
                    avg_time_on_page_s=metrics.get("visit_duration", 0),
                    bounce_rate=metrics.get("bounce_rate"),
                    raw={"url": url, **metrics},
                )
            )
            written += 1

    log.info("analytics_pull_completed", posts=written)
    return written


async def _fetch_plausible(client, site_id: str, api_key: str, slug: str) -> dict:
    resp = await client.get(
        "https://plausible.io/api/v1/stats/aggregate",
        params={
            "site_id": site_id,
            "period": "30d",
            "metrics": "visitors,pageviews,bounce_rate,visit_duration",
            "filters": f"event:page==/blog/{slug}",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    return {k: v.get("value", 0) for k, v in resp.json().get("results", {}).items()}
