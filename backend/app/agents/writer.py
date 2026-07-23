"""Agent 4 — Writer Agent.

The expensive call. Runs on the SMART tier with adaptive thinking, produces
the full article as structured JSON, then verifies its own output before the
post is allowed downstream.

Verification is deliberately two-layered:

  * Deterministic — n-gram overlap against the source text. This catches
    copying regardless of what the model claims. A prompt saying "be original"
    is a request; an n-gram check is a measurement.
  * LLM QA — factual grounding, AI-voice detection, structure. Runs on the
    FAST tier because judging is cheaper than writing.

If word count comes in short (the single most common miss), we issue one
targeted expansion call rather than regenerating from scratch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from slugify import slugify

from app.agents.base import Agent, AgentContext, AgentError, record_cost
from app.config import settings
from app.core.logging_conf import get_logger
from app.db.models import Post, PostStatus, RawArticle, Source
from app.llm.factory import assert_within_budget, get_provider
from app.prompts.templates import (
    QA_SCHEMA,
    QA_SYSTEM,
    WRITER_SCHEMA,
    WRITER_SYSTEM,
    build_writer_prompt,
)
from app.services.vector import recent_published_titles

log = get_logger(__name__)

# Longest run of consecutive words allowed to match a source verbatim.
MAX_NGRAM_OVERLAP = 7
# Below this originality score the post is held for review instead of published.
MIN_ORIGINALITY = 0.75
MIN_FACTUAL = 0.80


@dataclass
class WrittenArticle:
    post_id: str
    title: str
    slug: str
    word_count: int
    originality: float
    publishable: bool
    qa: dict = field(default_factory=dict)


class WriterAgent(Agent[dict, WrittenArticle]):
    name = "writer"
    optional = False

    async def execute(self, ctx: AgentContext, payload: dict) -> WrittenArticle:
        await assert_within_budget()

        story = payload["story"]
        primary, supporting = await self._load_sources(ctx, story)
        recent = await recent_published_titles(ctx.db, days=30)

        prompt = build_writer_prompt(
            angle=story.get("angle") or f"Analyse: {primary['title']}",
            primary=primary,
            supporting=supporting,
            recent_titles=recent,
            min_words=settings.ARTICLE_MIN_WORDS,
            max_words=settings.ARTICLE_MAX_WORDS,
        )

        provider = get_provider()
        system = WRITER_SYSTEM.format(
            min_words=settings.ARTICLE_MIN_WORDS, max_words=settings.ARTICLE_MAX_WORDS
        )

        resp = await provider.complete(
            system=system,
            prompt=prompt,
            tier="smart",
            max_tokens=32000,   # room for thinking + a 2500-word article
            json_schema=WRITER_SCHEMA,
        )
        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)

        article = resp.parsed
        if not article:
            raise AgentError(self.name, "writer returned unparseable output", recoverable=False)

        # ---- length repair ---------------------------------------------
        words = _word_count(article["body_markdown"])
        if words < settings.ARTICLE_MIN_WORDS * 0.9:
            log.warning("article_short", words=words, target=settings.ARTICLE_MIN_WORDS)
            article = await self._expand(ctx, provider, system, prompt, article, words)
            words = _word_count(article["body_markdown"])

        # ---- verification ----------------------------------------------
        source_texts = [
            (s.get("content") or s.get("description") or "") for s in [primary, *supporting]
        ]
        originality, worst = _originality_score(article["body_markdown"], source_texts)
        qa = await self._qa(ctx, provider, article, source_texts)

        publishable = (
            qa.get("publishable", False)
            and originality >= MIN_ORIGINALITY
            and qa.get("factual_grounding", 0) >= MIN_FACTUAL
        )

        post = await self._persist(
            ctx, story, article, resp, words, originality, worst, qa, publishable
        )

        log.info(
            "article_written",
            post_id=str(post.id),
            words=words,
            originality=round(originality, 3),
            factual=qa.get("factual_grounding"),
            publishable=publishable,
            cost_usd=round(resp.usage.cost_usd, 4),
        )
        return WrittenArticle(
            post_id=str(post.id),
            title=post.title,
            slug=post.slug,
            word_count=words,
            originality=originality,
            publishable=publishable,
            qa=qa,
        )

    # ------------------------------------------------------------- sources
    async def _load_sources(self, ctx: AgentContext, story: dict) -> tuple[dict, list[dict]]:
        ids = [story["article_id"], *story.get("supporting_ids", [])]
        rows = (
            await ctx.db.execute(
                select(RawArticle, Source)
                .join(Source, Source.id == RawArticle.source_id)
                .where(RawArticle.id.in_(ids))
            )
        ).all()

        by_id = {
            str(a.id): {
                "title": a.title,
                "url": a.url,
                "source": s.name,
                "description": a.description,
                "content": a.content,
                "published_at": a.published_at.isoformat() if a.published_at else None,
            }
            for a, s in rows
        }
        primary = by_id.get(story["article_id"])
        if not primary:
            raise AgentError(self.name, f"primary article {story['article_id']} missing",
                             recoverable=False)
        supporting = [by_id[i] for i in story.get("supporting_ids", []) if i in by_id]
        return primary, supporting

    # -------------------------------------------------------------- expand
    async def _expand(
        self, ctx: AgentContext, provider, system: str, original_prompt: str,
        article: dict, current_words: int,
    ) -> dict:
        """One targeted expansion pass. Cheaper and more reliable than a full
        regeneration, and it keeps the parts that were already good."""
        deficit = settings.ARTICLE_MIN_WORDS - current_words
        expand_prompt = (
            f"{original_prompt}\n\n"
            f"# REVISION REQUIRED\n\n"
            f"Your previous draft's body was {current_words} words — about "
            f"{deficit} short of the {settings.ARTICLE_MIN_WORDS} minimum.\n\n"
            f"Return the complete article again with the body expanded to "
            f"{settings.ARTICLE_MIN_WORDS}-{settings.ARTICLE_MAX_WORDS} words. "
            f"Add depth, not padding: develop the technical analysis further, "
            f"add concrete implications, expand the comparison to alternatives. "
            f"Do NOT add filler sentences, restate points already made, or pad "
            f"the conclusion.\n\n"
            f"Previous body for reference:\n\n{article['body_markdown']}"
        )
        try:
            resp = await provider.complete(
                system=system, prompt=expand_prompt, tier="smart",
                max_tokens=32000, json_schema=WRITER_SCHEMA,
            )
        except Exception as exc:
            log.warning("expansion_failed", error=str(exc))
            return article

        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)
        return resp.parsed or article

    # ------------------------------------------------------------------ qa
    async def _qa(self, ctx: AgentContext, provider, article: dict,
                  source_texts: list[str]) -> dict:
        sources_blob = "\n\n---\n\n".join(t[:3000] for t in source_texts if t)
        prompt = (
            f"# SOURCE MATERIAL\n\n{sources_blob}\n\n"
            f"# ARTICLE UNDER REVIEW\n\n"
            f"Title: {article['title']}\n"
            f"Subtitle: {article['subtitle']}\n\n"
            f"{article['body_markdown']}\n\n"
            f"## Expert opinion\n{article['expert_opinion']}\n\n"
            f"## Industry impact\n{article['industry_impact']}\n\n"
            f"## Predictions\n{article['future_predictions']}\n"
        )
        try:
            resp = await provider.complete(
                system=QA_SYSTEM, prompt=prompt, tier="fast",
                max_tokens=6000, json_schema=QA_SCHEMA,
            )
        except Exception as exc:
            # QA is a gate, not a generator. If it cannot run, fail closed:
            # mark not-publishable so a human looks at the post.
            log.error("qa_failed", error=str(exc))
            return {"publishable": False, "summary": f"QA unavailable: {exc}",
                    "factual_grounding": 0.0, "issues": []}

        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)
        return resp.parsed or {"publishable": False, "factual_grounding": 0.0, "issues": []}

    # ------------------------------------------------------------- persist
    async def _persist(self, ctx, story, article, resp, words, originality,
                       worst_overlap, qa, publishable) -> Post:
        base_slug = slugify(article["title"])[:180]
        slug = await _unique_slug(ctx, base_slug)

        status = PostStatus.draft
        if publishable:
            status = (
                PostStatus.ready_for_review if settings.HUMAN_REVIEW else PostStatus.approved
            )

        post = Post(
            run_id=ctx.run_id,
            title=article["title"],
            subtitle=article["subtitle"],
            slug=slug,
            executive_summary=article["executive_summary"],
            body_markdown=article["body_markdown"],
            highlights=article["highlights"],
            key_takeaways=article["key_takeaways"],
            expert_opinion=article["expert_opinion"],
            industry_impact=article["industry_impact"],
            future_predictions=article["future_predictions"],
            word_count=words,
            reading_time_minutes=max(1, round(words / 225)),
            category=article.get("category"),
            status=status,
            source_article_ids=[story["article_id"], *story.get("supporting_ids", [])],
            citations=article.get("citations", []),
            originality_score=round(originality, 4),
            max_source_similarity=round(worst_overlap, 4),
            quality_notes=qa,
            provider=resp.provider,
            model=resp.model,
            cost_usd=resp.usage.cost_usd,
        )
        ctx.db.add(post)
        await ctx.db.flush()
        return post


# ---------------------------------------------------------------- helpers
_WORDS = re.compile(r"\b[\w'-]+\b")
_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_MD_SYNTAX = re.compile(r"[#*_>`\[\]()!-]")


def _word_count(markdown: str) -> int:
    text = _CODE_BLOCK.sub(" ", markdown)
    return len(_WORDS.findall(_MD_SYNTAX.sub(" ", text)))


def _tokens(text: str) -> list[str]:
    return _WORDS.findall(text.lower())


def _originality_score(article_md: str, source_texts: list[str]) -> tuple[float, float]:
    """Deterministic plagiarism check.

    Builds the set of every n-gram in the source material, then measures what
    fraction of the article's n-grams appear in it. Independent of what the
    model asserts about its own originality.

    Returns (originality 0-1, worst overlap ratio 0-1).
    """
    article_tokens = _tokens(_CODE_BLOCK.sub(" ", article_md))
    n = MAX_NGRAM_OVERLAP
    if len(article_tokens) < n:
        return 1.0, 0.0

    source_ngrams: set[tuple[str, ...]] = set()
    for text in source_texts:
        tokens = _tokens(text)
        source_ngrams.update(
            tuple(tokens[i : i + n]) for i in range(max(0, len(tokens) - n + 1))
        )
    if not source_ngrams:
        return 1.0, 0.0

    article_ngrams = [
        tuple(article_tokens[i : i + n]) for i in range(len(article_tokens) - n + 1)
    ]
    matches = sum(1 for g in article_ngrams if g in source_ngrams)
    overlap = matches / len(article_ngrams)
    return round(1.0 - overlap, 4), round(overlap, 4)


async def _unique_slug(ctx: AgentContext, base: str) -> str:
    """Slugs are permanent public URLs, so collisions get a suffix rather than
    overwriting an existing post."""
    slug = base
    for attempt in range(1, 20):
        exists = await ctx.db.scalar(select(Post.id).where(Post.slug == slug))
        if not exists:
            return slug
        slug = f"{base}-{attempt}"
    return f"{base}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
