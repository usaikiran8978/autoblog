"""Agent 7 — Publisher Agent.

Fans out to every configured target. Each target gets its own `publications`
row, its own retry budget and its own status, so partial success is a
first-class outcome rather than an ambiguous one.

Idempotency: a target already marked `success` for this post is skipped, so
re-running a partially-failed publish never double-posts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents.base import Agent, AgentContext
from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import POSTS_PUBLISHED
from app.db.models import Post, PostStatus, Publication, PublicationStatus
from app.services.publishers.base import Publisher
from app.services.publishers.targets import get_publishers

log = get_logger(__name__)


@dataclass(slots=True)
class PublishSummary:
    post_id: str
    succeeded: list[str]
    failed: list[str]
    urls: dict[str, str]


class PublisherAgent(Agent[str, PublishSummary]):
    name = "publisher"
    optional = False

    async def execute(self, ctx: AgentContext, post_id: str) -> PublishSummary:
        post = (
            await ctx.db.execute(
                select(Post)
                .where(Post.id == post_id)
                .options(
                    selectinload(Post.images),
                    selectinload(Post.seo),
                    selectinload(Post.publications),
                )
            )
        ).scalar_one_or_none()
        if not post:
            raise ValueError(f"post {post_id} not found")

        if post.status not in (PostStatus.approved, PostStatus.publishing):
            log.warning("publish_skipped_status", post_id=post_id, status=post.status.value)
            return PublishSummary(post_id, [], [], {})

        post.status = PostStatus.publishing
        await ctx.db.flush()

        already = {p.target for p in post.publications if p.status == PublicationStatus.success}
        targets = [p for p in get_publishers() if p.target not in already]

        if not targets:
            log.info("publish_nothing_to_do", post_id=post_id, already=list(already))
            return PublishSummary(post_id, list(already), [], {})

        results = await asyncio.gather(
            *(self._publish_one(ctx, post, t) for t in targets), return_exceptions=True
        )

        succeeded, failed, urls = list(already), [], {}
        for publisher, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                failed.append(publisher.target)
                POSTS_PUBLISHED.labels(publisher.target, "error").inc()
            else:
                succeeded.append(publisher.target)
                if result:
                    urls[publisher.target] = result
                POSTS_PUBLISHED.labels(publisher.target, "ok").inc()

        # Any successful target counts as published — a failed secondary
        # syndication should not hold back the primary site.
        if succeeded:
            post.status = PostStatus.published
            post.published_at = post.published_at or datetime.now(timezone.utc)
        else:
            post.status = PostStatus.failed

        await ctx.db.flush()
        log.info("publish_completed", post_id=post_id, succeeded=succeeded, failed=failed)
        return PublishSummary(post_id, succeeded, failed, urls)

    async def _publish_one(
        self, ctx: AgentContext, post: Post, publisher: Publisher
    ) -> str | None:
        record = next(
            (p for p in post.publications if p.target == publisher.target), None
        )
        if record is None:
            record = Publication(post_id=post.id, target=publisher.target)
            ctx.db.add(record)

        record.attempts += 1
        try:
            result = await publisher.publish(post)
        except Exception as exc:
            record.status = PublicationStatus.failed
            record.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            log.error("publish_target_failed", target=publisher.target,
                      post_id=str(post.id), error=str(exc))
            raise

        record.status = PublicationStatus.success
        record.external_id = result.external_id
        record.external_url = result.external_url
        record.response_payload = _truncate_payload(result.raw)
        record.published_at = datetime.now(timezone.utc)
        record.last_error = None

        # WordPress returns the real attachment ID; store it so a future edit
        # can reuse the media instead of re-uploading.
        for image in post.images:
            if publisher.target == "wordpress" and not image.remote_media_id:
                image.remote_media_id = str(result.raw.get("featured_media") or "") or None

        log.info("publish_target_ok", target=publisher.target,
                 post_id=str(post.id), url=result.external_url)
        return result.external_url


def _truncate_payload(payload: dict) -> dict:
    """CMS responses can embed the entire rendered post; keep the useful keys."""
    keep = {"id", "link", "url", "status", "slug", "date", "modified"}
    return {k: v for k, v in payload.items() if k in keep and not isinstance(v, (dict, list))}
