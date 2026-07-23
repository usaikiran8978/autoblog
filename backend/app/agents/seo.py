"""Agent 5 — SEO Agent.

The LLM writes the human-facing strings (title, description, keywords, FAQ).
Everything structural — JSON-LD, Open Graph, Twitter Card — is assembled
deterministically in Python. Schema.org markup that a model improvises tends
to be subtly invalid, and invalid structured data is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.agents.base import Agent, AgentContext, record_cost
from app.config import settings
from app.core.logging_conf import get_logger
from app.db.models import Post, PostSEO
from app.llm.factory import get_provider
from app.prompts.templates import SEO_SCHEMA, SEO_SYSTEM

log = get_logger(__name__)

TITLE_MAX = 60
DESC_MAX = 155


@dataclass(slots=True)
class SEOResult:
    seo_title: str
    meta_description: str
    canonical_url: str
    keywords: list[str]
    faq_count: int


class SEOAgent(Agent[str, SEOResult]):
    name = "seo"
    optional = True  # a post without perfect metadata still beats no post

    async def execute(self, ctx: AgentContext, post_id: str) -> SEOResult:
        post = await ctx.db.get(Post, post_id)
        if not post:
            raise ValueError(f"post {post_id} not found")

        provider = get_provider()
        prompt = (
            f"Title: {post.title}\n"
            f"Subtitle: {post.subtitle}\n"
            f"Category: {post.category}\n"
            f"Current slug: {post.slug}\n\n"
            f"Executive summary:\n{post.executive_summary}\n\n"
            f"Key takeaways:\n"
            + "\n".join(f"- {t}" for t in (post.key_takeaways or []))
            + f"\n\nArticle body (truncated):\n{post.body_markdown[:6000]}"
        )

        resp = await provider.complete(
            system=SEO_SYSTEM, prompt=prompt, tier="fast",
            max_tokens=4000, json_schema=SEO_SCHEMA,
        )
        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)

        data = resp.parsed or {}
        seo_title = _truncate(data.get("seo_title") or post.title, TITLE_MAX)
        meta_desc = _truncate(
            data.get("meta_description") or post.executive_summary or "", DESC_MAX
        )
        canonical = f"{settings.SITE_BASE_URL.rstrip('/')}/blog/{post.slug}"

        image_url = next(
            (i.public_url for i in post.images if i.role == "featured" and i.public_url), None
        )
        published = post.published_at or datetime.now(timezone.utc)

        seo = PostSEO(
            post_id=post.id,
            seo_title=seo_title,
            meta_description=meta_desc,
            canonical_url=canonical,
            focus_keyword=data.get("focus_keyword"),
            keywords=data.get("keywords", []),
            faq=data.get("faq", []),
            json_ld=_build_json_ld(post, seo_title, meta_desc, canonical, image_url,
                                   published, data.get("keywords", []), data.get("faq", [])),
            og_tags=_build_og(seo_title, meta_desc, canonical, image_url, published, post),
            twitter_card=_build_twitter(seo_title, meta_desc, image_url),
        )
        ctx.db.add(seo)
        await ctx.db.flush()

        log.info("seo_generated", post_id=post_id, keywords=len(seo.keywords),
                 faq=len(seo.faq))
        return SEOResult(seo_title, meta_desc, canonical, seo.keywords, len(seo.faq))


def _truncate(text: str, limit: int) -> str:
    """Cut on a word boundary — a description ending mid-word looks broken in
    a SERP."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—-")


def _build_json_ld(post, title, description, canonical, image, published,
                   keywords, faq) -> dict:
    """@graph with BlogPosting + BreadcrumbList + optional FAQPage.

    A single @graph is preferred over three separate script tags — it lets the
    nodes reference each other by @id and is what Google's own examples use.
    """
    graph: list[dict] = [
        {
            "@type": "BlogPosting",
            "@id": f"{canonical}#article",
            "headline": title[:110],  # schema.org headline limit
            "description": description,
            "url": canonical,
            "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
            "datePublished": published.isoformat(),
            "dateModified": datetime.now(timezone.utc).isoformat(),
            "wordCount": post.word_count,
            "timeRequired": f"PT{post.reading_time_minutes}M",
            "articleSection": post.category,
            "keywords": ", ".join(keywords[:10]),
            "inLanguage": "en-US",
            "author": {
                "@type": "Organization",
                "name": settings.AUTHOR_NAME,
                "url": settings.SITE_BASE_URL,
            },
            "publisher": {
                "@type": "Organization",
                "name": settings.SITE_NAME,
                "url": settings.SITE_BASE_URL,
                "logo": {
                    "@type": "ImageObject",
                    "url": f"{settings.SITE_BASE_URL.rstrip('/')}/logo.png",
                },
            },
            **({"image": {"@type": "ImageObject", "url": image}} if image else {}),
            # Disclosure: material fact for both readers and search engines.
            "creativeWorkStatus": "Published",
            "isBasedOn": [c.get("url") for c in (post.citations or []) if c.get("url")],
        },
        {
            "@type": "BreadcrumbList",
            "@id": f"{canonical}#breadcrumb",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home",
                 "item": settings.SITE_BASE_URL},
                {"@type": "ListItem", "position": 2, "name": "Blog",
                 "item": f"{settings.SITE_BASE_URL.rstrip('/')}/blog"},
                {"@type": "ListItem", "position": 3, "name": post.title, "item": canonical},
            ],
        },
    ]

    if faq:
        graph.append({
            "@type": "FAQPage",
            "@id": f"{canonical}#faq",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": item["question"],
                    "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
                }
                for item in faq
            ],
        })

    return {"@context": "https://schema.org", "@graph": graph}


def _build_og(title, description, canonical, image, published, post) -> dict:
    tags = {
        "og:type": "article",
        "og:title": title,
        "og:description": description,
        "og:url": canonical,
        "og:site_name": settings.SITE_NAME,
        "og:locale": "en_US",
        "article:published_time": published.isoformat(),
        "article:author": settings.AUTHOR_NAME,
        "article:section": post.category or "Technology",
    }
    if image:
        tags |= {
            "og:image": image,
            "og:image:width": "1536",
            "og:image:height": "1024",
            "og:image:alt": title,
        }
    return tags


def _build_twitter(title, description, image) -> dict:
    card = {
        "twitter:card": "summary_large_image" if image else "summary",
        "twitter:title": title,
        "twitter:description": description,
    }
    if image:
        card["twitter:image"] = image
    return card
