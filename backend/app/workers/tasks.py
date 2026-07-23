"""Celery tasks. Thin wrappers — all real logic lives in agents/services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, func, select, update
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import QUEUE_DEPTH
from app.db.models import (
    AnalyticsSnapshot,
    IdempotencyKey,
    PipelineRun,
    Post,
    PostStatus,
    Publication,
    PublicationStatus,
    RawArticle,
    RunStatus,
    Source,
)
from app.db.session import run_async, session_scope

log = get_logger(__name__)


# ---------------------------------------------------------------- pipeline
@shared_task(
    bind=True,
    name="app.workers.tasks.run_pipeline",
    max_retries=2,
    default_retry_delay=300,
)
def run_pipeline(self, trigger: str = "schedule", posts: int | None = None) -> dict:
    """The twice-daily publish cycle."""
    from app.agents.coordinator import Coordinator

    slot, key = _slot_and_key(trigger)

    try:
        result = run_async(
            Coordinator().run(
                trigger=trigger,
                slot=slot,
                idempotency_key=key,
                posts_per_run=posts,
            )
        )
    except SoftTimeLimitExceeded:
        log.error("pipeline_soft_timeout", task_id=self.request.id)
        raise
    except Exception as exc:
        log.error("pipeline_task_failed", error=str(exc), retries=self.request.retries)
        # Retry infrastructure failures; a genuine content failure will fail
        # again the same way and stop after max_retries.
        raise self.retry(exc=exc) from exc

    payload = {
        "run_id": result.run_id,
        "status": result.status,
        "posts": len(result.posts),
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "warnings": result.warnings,
    }
    if result.status in ("failed", "partial"):
        run_async(_alert(f"Pipeline {result.status}", payload))
    return payload


def _slot_and_key(trigger: str) -> tuple[str | None, str | None]:
    """Derive a stable idempotency key per scheduled slot.

    Two beat processes (or a beat restart at the fire minute) would otherwise
    publish the same edition twice.
    """
    if trigger != "schedule":
        return None, None
    now = datetime.now(ZoneInfo(settings.TIMEZONE))
    slot = "morning" if now.hour < 12 else "evening"
    return slot, f"pipeline:{now:%Y-%m-%d}:{slot}"


@shared_task(name="app.workers.tasks.publish_post", max_retries=3, bind=True)
def publish_post(self, post_id: str) -> dict:
    """Publish a single approved post (manual/editor-triggered path)."""
    from app.agents.coordinator import publish_approved_post

    try:
        return run_async(publish_approved_post(post_id))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120 * (self.request.retries + 1)) from exc


# ------------------------------------------------------------- maintenance
@shared_task(name="app.workers.tasks.retry_failed_publications")
def retry_failed_publications() -> dict:
    """Retry queue for publish failures.

    Exponential-ish backoff via attempt count, capped at 5 attempts so a
    permanently misconfigured target stops burning cycles.
    """
    async def _run() -> dict:
        from app.agents.base import AgentContext
        from app.agents.publisher import PublisherAgent

        async with session_scope() as db:
            rows = (
                await db.execute(
                    select(Publication.post_id)
                    .where(
                        Publication.status == PublicationStatus.failed,
                        Publication.attempts < 5,
                        Publication.updated_at
                        < datetime.now(timezone.utc) - timedelta(minutes=15),
                    )
                    .distinct()
                    .limit(10)
                )
            ).scalars().all()

            retried = []
            for post_id in rows:
                post = await db.get(Post, post_id)
                if not post:
                    continue
                post.status = PostStatus.approved  # re-arm for the publisher
                ctx = AgentContext(run_id=post.run_id or post.id, db=db)
                try:
                    await PublisherAgent().run(ctx, str(post_id))
                    retried.append(str(post_id))
                except Exception as exc:
                    log.warning("publication_retry_failed", post_id=str(post_id),
                                error=str(exc))
            return {"retried": retried}

    return run_async(_run())


@shared_task(name="app.workers.tasks.refresh_sources")
def refresh_sources() -> dict:
    """Re-enable sources whose circuit has had time to recover, so a transient
    multi-hour outage does not permanently remove a feed."""

    async def _run() -> dict:
        async with session_scope() as db:
            result = await db.execute(
                update(Source)
                .where(
                    Source.enabled.is_(False),
                    Source.consecutive_failures >= 10,
                    Source.last_fetched_at
                    < datetime.now(timezone.utc) - timedelta(days=1),
                )
                .values(enabled=True, consecutive_failures=0, last_error=None)
            )
            return {"reenabled": result.rowcount}

    return run_async(_run())


@shared_task(name="app.workers.tasks.pull_analytics")
def pull_analytics() -> dict:
    from app.agents.analytics import collect_post_metrics

    async def _run() -> dict:
        async with session_scope() as db:
            return {"snapshots": await collect_post_metrics(db)}

    return run_async(_run())


@shared_task(name="app.workers.tasks.prune")
def prune() -> dict:
    """Weekly retention sweep.

    Raw articles are the bulk of the row count and have no value after ~90
    days. Posts and analytics are never auto-deleted.
    """

    async def _run() -> dict:
        now = datetime.now(timezone.utc)
        async with session_scope() as db:
            articles = await db.execute(
                delete(RawArticle).where(RawArticle.collected_at < now - timedelta(days=90))
            )
            runs = await db.execute(
                delete(PipelineRun).where(
                    PipelineRun.created_at < now - timedelta(days=180),
                    PipelineRun.status.in_([RunStatus.failed, RunStatus.cancelled]),
                )
            )
            keys = await db.execute(
                delete(IdempotencyKey).where(IdempotencyKey.expires_at < now)
            )
            snapshots = await db.execute(
                delete(AnalyticsSnapshot).where(
                    AnalyticsSnapshot.captured_at < now - timedelta(days=400)
                )
            )
            return {
                "raw_articles": articles.rowcount,
                "pipeline_runs": runs.rowcount,
                "idempotency_keys": keys.rowcount,
                "analytics_snapshots": snapshots.rowcount,
            }

    return run_async(_run())


@shared_task(name="app.workers.tasks.export_queue_depth")
def export_queue_depth() -> dict:
    """Publish queue depth to Prometheus. Rising depth is the earliest signal
    that workers are undersized or wedged."""
    import redis

    r = redis.from_url(settings.broker_url)
    depths = {}
    for queue in ("pipeline", "maintenance"):
        depth = r.llen(queue)
        QUEUE_DEPTH.labels(queue).set(depth)
        depths[queue] = depth
    return depths


@shared_task(name="app.workers.tasks.seed_sources")
def seed_sources() -> dict:
    """Idempotent seed of the source registry."""

    async def _run() -> dict:
        from app.db.models import SourceKind
        from app.services.source_registry import SEED_SOURCES

        created = updated = 0
        async with session_scope() as db:
            for entry in SEED_SOURCES:
                existing = await db.scalar(
                    select(Source).where(Source.slug == entry["slug"])
                )
                if existing:
                    existing.url = entry["url"]
                    existing.trust_score = entry["trust"]
                    existing.categories = entry["categories"]
                    existing.config = entry.get("config", {})
                    updated += 1
                else:
                    db.add(
                        Source(
                            slug=entry["slug"],
                            name=entry["name"],
                            kind=SourceKind(entry["kind"]),
                            url=entry["url"],
                            categories=entry["categories"],
                            trust_score=entry["trust"],
                            config=entry.get("config", {}),
                        )
                    )
                    created += 1
        return {"created": created, "updated": updated}

    return run_async(_run())


async def _alert(title: str, payload: dict) -> None:
    """Fire a webhook on pipeline failure. Slack-compatible shape."""
    if not settings.ALERT_WEBHOOK_URL:
        return
    from app.core.resilience import http_client

    lines = "\n".join(f"• *{k}*: {v}" for k, v in payload.items())
    try:
        async with http_client() as client:
            await client.post(
                settings.ALERT_WEBHOOK_URL,
                json={"text": f"*{title}* ({settings.ENV})\n{lines}"},
            )
    except Exception as exc:
        log.warning("alert_delivery_failed", error=str(exc))


async def pipeline_health() -> dict:
    """Used by /health/deep — did we actually publish on schedule?"""
    async with session_scope() as db:
        last = await db.scalar(
            select(PipelineRun).order_by(PipelineRun.created_at.desc()).limit(1)
        )
        published_24h = await db.scalar(
            select(func.count(Post.id)).where(
                Post.status == PostStatus.published,
                Post.published_at > datetime.now(timezone.utc) - timedelta(hours=24),
            )
        )
    return {
        "last_run_at": last.created_at.isoformat() if last else None,
        "last_run_status": last.status.value if last else None,
        "posts_published_24h": published_24h or 0,
    }
