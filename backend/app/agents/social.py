"""Agent 8 — Social Agent.

Generates platform-native copy for LinkedIn, X/Twitter, Facebook, Threads and
Instagram in a single call, then enforces each platform's hard character limit
in Python. Models routinely overshoot limits by 10-20%, and a post rejected at
publish time is worse than one trimmed here.

Copy is generated and stored, not auto-posted. Scheduling to the actual
networks is a separate concern (Buffer/Hootsuite/native APIs) and is left as
an integration point — see docs/BLUEPRINT.md §15.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents.base import Agent, AgentContext, record_cost
from app.core.logging_conf import get_logger
from app.core.metrics import SOCIAL_POSTS
from app.db.models import Post, SocialPost
from app.llm.factory import get_provider
from app.prompts.templates import SOCIAL_SCHEMA, SOCIAL_SYSTEM

log = get_logger(__name__)

# Hard platform limits. We trim to slightly under to leave room for the URL.
LIMITS = {
    "linkedin": 3000,
    "twitter": 280,
    "facebook": 63206,
    "threads": 500,
    "instagram": 2200,
}
# Reserve for a shortened link appended at post time.
URL_RESERVE = 25


@dataclass(slots=True)
class SocialResult:
    post_id: str
    platforms: list[str]


class SocialAgent(Agent[str, SocialResult]):
    name = "social"
    optional = True

    async def execute(self, ctx: AgentContext, post_id: str) -> SocialResult:
        post = (
            await ctx.db.execute(
                select(Post).where(Post.id == post_id).options(selectinload(Post.seo))
            )
        ).scalar_one_or_none()
        if not post:
            raise ValueError(f"post {post_id} not found")

        url = (
            post.seo.canonical_url
            if post.seo and post.seo.canonical_url
            else f"/blog/{post.slug}"
        )

        provider = get_provider()
        resp = await provider.complete(
            system=SOCIAL_SYSTEM,
            prompt=(
                f"Article title: {post.title}\n"
                f"Subtitle: {post.subtitle}\n"
                f"Category: {post.category}\n"
                f"URL: {url}\n\n"
                f"Summary:\n{post.executive_summary}\n\n"
                f"Highlights:\n"
                + "\n".join(f"- {h}" for h in (post.highlights or []))
                + "\n\nKey takeaways:\n"
                + "\n".join(f"- {t}" for t in (post.key_takeaways or []))
                + f"\n\nThe most interesting claim in the piece:\n"
                f"{(post.expert_opinion or '')[:600]}"
            ),
            tier="fast",
            max_tokens=6000,
            json_schema=SOCIAL_SCHEMA,
        )
        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)

        data = resp.parsed or {}
        created: list[str] = []

        for platform, limit in LIMITS.items():
            payload = data.get(platform)
            if not payload:
                continue

            body = payload.get("body", "").strip()
            hashtags = [_normalize_tag(h) for h in payload.get("hashtags", [])]

            # X/Twitter: the model returns a hook + thread. Store the hook as
            # the body and keep the thread in `cta` metadata order.
            if platform == "twitter" and payload.get("thread"):
                thread = [t.strip() for t in payload["thread"] if t.strip()]
                body = "\n\n---\n\n".join(
                    [_fit(body, limit - URL_RESERVE), *[_fit(t, limit) for t in thread]]
                )
            else:
                body = _fit(body, limit - URL_RESERVE)

            record = await ctx.db.scalar(
                select(SocialPost).where(
                    SocialPost.post_id == post.id, SocialPost.platform == platform
                )
            )
            if record is None:
                record = SocialPost(post_id=post.id, platform=platform)
                ctx.db.add(record)

            record.body = body
            record.hashtags = hashtags
            record.cta = payload.get("cta")
            record.char_count = len(body)
            created.append(platform)
            SOCIAL_POSTS.labels(platform).inc()

        await ctx.db.flush()
        log.info("social_generated", post_id=post_id, platforms=created)
        return SocialResult(post_id, created)


def _fit(text: str, limit: int) -> str:
    """Trim to the limit on a sentence boundary where possible, word boundary
    otherwise. Never mid-word — that reads as a bug to every reader."""
    text = text.strip()
    if len(text) <= limit:
        return text

    window = text[:limit]
    for terminator in (". ", "! ", "? ", "\n"):
        idx = window.rfind(terminator)
        if idx > limit * 0.6:
            return window[: idx + 1].strip()
    return window.rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def _normalize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#")
    return f"#{''.join(c for c in tag if c.isalnum())}" if tag else ""
