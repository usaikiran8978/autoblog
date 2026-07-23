"""Agent 10 — Coordinator.

Owns the pipeline as an explicit, deterministic state machine. This is
deliberately *not* an LLM-driven planner: the sequence of steps is known in
advance and never varies, so making a model decide it each run would add cost,
latency and nondeterminism while removing the ability to resume mid-pipeline.

Failure policy:
  * `optional=False` agents (collector, dedupe, ranker, writer, publisher)
    abort the run.
  * `optional=True` agents (seo, image, social, analytics) log, mark the run
    `partial`, and continue. A post without a hero image is still a post.

Concurrency: SEO and image generation are independent, so they run together.
That is worth roughly 20-30 seconds of wall clock per post.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agents.analytics import AnalyticsAgent
from app.agents.base import AgentContext, AgentError, finalize_run
from app.agents.collector import CollectorAgent
from app.agents.deduplicator import DeduplicatorAgent
from app.agents.image_agent import ImageAgent
from app.agents.publisher import PublisherAgent
from app.agents.ranker import RankingAgent
from app.agents.seo import SEOAgent
from app.agents.social import SocialAgent
from app.agents.writer import WriterAgent
from app.config import settings
from app.core.logging_conf import get_logger, run_id_ctx
from app.core.metrics import PIPELINE_DURATION, PIPELINE_RUNS
from app.db.models import IdempotencyKey, PipelineRun, Post, PostStatus, RunStatus
from app.db.session import session_scope
from app.llm.factory import BudgetExceeded, assert_within_budget

log = get_logger(__name__)


@dataclass
class PipelineResult:
    run_id: str
    status: str
    posts: list[dict] = field(default_factory=list)
    collected: int = 0
    after_dedupe: int = 0
    ranked: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class Coordinator:
    """Entry point for a full publish cycle."""

    def __init__(self) -> None:
        self.collector = CollectorAgent()
        self.deduplicator = DeduplicatorAgent()
        self.ranker = RankingAgent()
        self.writer = WriterAgent()
        self.seo = SEOAgent()
        self.image = ImageAgent()
        self.social = SocialAgent()
        self.publisher = PublisherAgent()
        self.analytics = AnalyticsAgent()

    async def run(
        self,
        *,
        trigger: str = "schedule",
        slot: str | None = None,
        idempotency_key: str | None = None,
        posts_per_run: int | None = None,
    ) -> PipelineResult:
        if idempotency_key and (existing := await self._existing_run(idempotency_key)):
            log.info("pipeline_deduplicated", key=idempotency_key, run_id=str(existing))
            return PipelineResult(run_id=str(existing), status="duplicate")

        started = datetime.now(timezone.utc)
        warnings: list[str] = []

        async with session_scope() as db:
            run = PipelineRun(
                trigger=trigger, slot=slot, status=RunStatus.running, started_at=started
            )
            db.add(run)
            await db.flush()
            run_id = run.id

            if idempotency_key:
                db.add(
                    IdempotencyKey(
                        key=idempotency_key,
                        run_id=run_id,
                        expires_at=started + timedelta(hours=6),
                    )
                )

        token = run_id_ctx.set(str(run_id))
        log.info("pipeline_started", trigger=trigger, slot=slot)

        result = PipelineResult(run_id=str(run_id), status="running")

        try:
            async with session_scope() as db:
                ctx = AgentContext(run_id=run_id, db=db)
                await assert_within_budget()

                # ---- 1. collect -----------------------------------------
                collection = await self.collector.run(ctx, None)
                ctx.state["collected"] = collection.collected
                result.collected = collection.collected
                if collection.failed_sources:
                    warnings.append(
                        f"{len(collection.failed_sources)} source(s) failed: "
                        + ", ".join(collection.failed_sources[:5])
                    )

                # ---- 2. deduplicate -------------------------------------
                dedupe = await self.deduplicator.run(ctx, collection.article_ids)
                ctx.state["after_dedupe"] = len(dedupe.unique_ids)
                result.after_dedupe = len(dedupe.unique_ids)

                if not dedupe.unique_ids:
                    raise AgentError("coordinator", "no unique articles after dedupe",
                                     recoverable=False)

                # ---- 3. rank --------------------------------------------
                ranked = await self.ranker.run(
                    ctx, {"unique_ids": dedupe.unique_ids, "clusters": dedupe.clusters}
                )
                ctx.state["ranked"] = len(ranked)
                result.ranked = len(ranked)
                if not ranked:
                    raise AgentError("coordinator", "ranker returned no stories",
                                     recoverable=False)

                # ---- 4-8. one post per selected story --------------------
                count = posts_per_run or settings.POSTS_PER_RUN
                for story in ranked[:count]:
                    try:
                        post_summary = await self._produce_post(ctx, asdict(story))
                        result.posts.append(post_summary)
                    except AgentError as exc:
                        if not exc.recoverable:
                            raise
                        warnings.append(str(exc))
                        log.error("post_production_failed", story=story.title, error=str(exc))

                # ---- 9. analytics ---------------------------------------
                try:
                    analytics = await self.analytics.run(ctx, None)
                    result.cost_usd = analytics.total_cost_usd
                    result.duration_seconds = analytics.duration_seconds
                except AgentError as exc:
                    warnings.append(str(exc))

                await finalize_run(db, run_id)

            status = RunStatus.partial if warnings else RunStatus.succeeded
            if not result.posts:
                status = RunStatus.failed

            await self._close_run(run_id, status, warnings=warnings)
            result.status = status.value
            result.warnings = warnings

            PIPELINE_RUNS.labels(trigger, status.value).inc()

        except BudgetExceeded as exc:
            await self._close_run(run_id, RunStatus.failed, error=str(exc))
            PIPELINE_RUNS.labels(trigger, "budget_exceeded").inc()
            result.status, result.error = "failed", str(exc)
            log.error("pipeline_budget_exceeded", error=str(exc))

        except Exception as exc:
            await self._close_run(run_id, RunStatus.failed, error=str(exc))
            PIPELINE_RUNS.labels(trigger, "failed").inc()
            result.status, result.error = "failed", str(exc)
            log.error("pipeline_failed", error=str(exc), exc_info=True)

        finally:
            duration = (datetime.now(timezone.utc) - started).total_seconds()
            PIPELINE_DURATION.observe(duration)
            result.duration_seconds = result.duration_seconds or round(duration, 1)
            run_id_ctx.reset(token)

        log.info("pipeline_finished", status=result.status, posts=len(result.posts),
                 cost_usd=result.cost_usd, duration_s=result.duration_seconds)
        return result

    # ------------------------------------------------------- single post
    async def _produce_post(self, ctx: AgentContext, story: dict) -> dict:
        written = await self.writer.run(ctx, {"story": story})
        post_id = written.post_id

        # SEO and imagery are independent — run them concurrently.
        seo_task = self.seo.run(ctx, post_id)
        image_task = self.image.run(ctx, post_id)
        seo_result, image_result = await asyncio.gather(
            seo_task, image_task, return_exceptions=True
        )

        warnings: list[str] = []
        for label, outcome in (("seo", seo_result), ("image", image_result)):
            if isinstance(outcome, BaseException):
                warnings.append(f"{label}: {outcome}")
                log.warning("optional_agent_failed", agent=label, error=str(outcome))

        # Social copy needs the canonical URL that SEO produces.
        try:
            await self.social.run(ctx, post_id)
        except AgentError as exc:
            warnings.append(f"social: {exc}")

        published: dict = {}
        if not written.publishable:
            log.warning("post_held_for_review", post_id=post_id,
                        originality=written.originality, qa=written.qa.get("summary"))
            warnings.append("held for review: failed QA gate")
        elif settings.HUMAN_REVIEW:
            log.info("post_awaiting_human_review", post_id=post_id)
        else:
            summary = await self.publisher.run(ctx, post_id)
            published = {"targets": summary.succeeded, "urls": summary.urls}

        return {
            "post_id": post_id,
            "title": written.title,
            "slug": written.slug,
            "word_count": written.word_count,
            "originality": written.originality,
            "publishable": written.publishable,
            "published": published,
            "warnings": warnings,
        }

    # ------------------------------------------------------------ helpers
    async def _existing_run(self, key: str) -> uuid.UUID | None:
        async with session_scope() as db:
            record = await db.get(IdempotencyKey, key)
            if record and record.expires_at > datetime.now(timezone.utc):
                return record.run_id
        return None

    async def _close_run(
        self, run_id: uuid.UUID, status: RunStatus, *,
        error: str | None = None, warnings: list[str] | None = None,
    ) -> None:
        async with session_scope() as db:
            run = await db.get(PipelineRun, run_id)
            if not run:
                return
            run.status = status
            run.finished_at = datetime.now(timezone.utc)
            if error:
                run.error = error[:2000]
            elif warnings:
                run.error = "; ".join(warnings)[:2000]


async def publish_approved_post(post_id: str) -> dict:
    """Manual publish path for HUMAN_REVIEW=true — called by
    POST /posts/{id}/publish after an editor approves."""
    async with session_scope() as db:
        post = await db.get(Post, post_id)
        if not post:
            raise ValueError(f"post {post_id} not found")
        if post.status not in (PostStatus.approved, PostStatus.ready_for_review):
            raise ValueError(f"post is {post.status.value}, expected approved/ready_for_review")

        post.status = PostStatus.approved
        ctx = AgentContext(run_id=post.run_id or uuid.uuid4(), db=db)
        summary = await PublisherAgent().run(ctx, post_id)
        return asdict(summary)
