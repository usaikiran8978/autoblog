"""Concrete publishing targets: WordPress, Ghost, Medium, custom CMS, Markdown.

Each adapter is independent and independently retried. A WordPress 500 must
not prevent the Ghost publish from succeeding, which is why `publications` is
one row per (post, target) rather than a single status on the post.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.resilience import http_client, with_retry
from app.db.models import Post
from app.services.publishers.base import PublishResult, Publisher, render_html

log = get_logger(__name__)


class WordPressPublisher(Publisher):
    """WP REST API v2 with Application Password auth.

    Two-step: upload the hero image to /media first so the post can reference
    a real attachment ID as its featured image, then create the post.
    """

    target = "wordpress"

    def is_configured(self) -> bool:
        return bool(
            settings.WORDPRESS_URL
            and settings.WORDPRESS_USERNAME
            and settings.WORDPRESS_PASSWORD
        )

    @property
    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{settings.WORDPRESS_USERNAME}:{settings.WORDPRESS_PASSWORD}".encode()
        ).decode()
        return {"Authorization": f"Basic {token}"}

    async def publish(self, post: Post) -> PublishResult:
        base = f"{settings.WORDPRESS_URL.rstrip('/')}/wp-json/wp/v2"

        async with http_client() as client:
            media_id = await self._upload_media(client, base, post)

            payload = {
                "title": post.title,
                "slug": post.slug,
                "content": render_html(post),
                "excerpt": (post.seo.meta_description if post.seo else post.executive_summary),
                "status": settings.PUBLISH_STATUS,
                "comment_status": "open",
                "meta": {
                    "_yoast_wpseo_title": post.seo.seo_title if post.seo else post.title,
                    "_yoast_wpseo_metadesc": post.seo.meta_description if post.seo else "",
                    "_yoast_wpseo_focuskw": post.seo.focus_keyword if post.seo else "",
                },
            }
            if media_id:
                payload["featured_media"] = media_id
            if post.seo and post.seo.keywords:
                payload["tags"] = []  # tag IDs must be resolved/created first; see docs

            async def _call():
                r = await client.post(f"{base}/posts", json=payload, headers=self._auth_header)
                r.raise_for_status()
                return r.json()

            data = await with_retry(_call, label="publish:wordpress")

        return PublishResult("wordpress", str(data["id"]), data.get("link"), data)

    async def _upload_media(self, client: httpx.AsyncClient, base: str, post: Post) -> int | None:
        featured = next((i for i in post.images if i.role == "featured"), None)
        if not featured or not featured.storage_path:
            return None
        path = Path(featured.storage_path)
        if not path.exists():
            return None

        try:
            r = await client.post(
                f"{base}/media",
                content=path.read_bytes(),
                headers={
                    **self._auth_header,
                    "Content-Type": "image/png",
                    "Content-Disposition": f'attachment; filename="{path.name}"',
                },
            )
            r.raise_for_status()
            media = r.json()
            # Alt text is a separate PATCH; skipping it hurts accessibility.
            await client.post(
                f"{base}/media/{media['id']}",
                json={"alt_text": featured.alt_text or post.title,
                      "caption": post.subtitle or ""},
                headers=self._auth_header,
            )
            return media["id"]
        except httpx.HTTPError as exc:
            log.warning("wp_media_upload_failed", error=str(exc))
            return None


class GhostPublisher(Publisher):
    """Ghost Admin API. Auth is a short-lived JWT signed with the key secret."""

    target = "ghost"

    def is_configured(self) -> bool:
        return bool(settings.GHOST_ADMIN_API_URL and settings.GHOST_ADMIN_API_KEY)

    def _token(self) -> str:
        key_id, secret = settings.GHOST_ADMIN_API_KEY.split(":")
        now = int(time.time())
        return jwt.encode(
            {"iat": now, "exp": now + 300, "aud": "/admin/"},
            bytes.fromhex(secret),
            algorithm="HS256",
            headers={"kid": key_id, "alg": "HS256", "typ": "JWT"},
        )

    async def publish(self, post: Post) -> PublishResult:
        url = f"{settings.GHOST_ADMIN_API_URL.rstrip('/')}/ghost/api/admin/posts/?source=html"
        featured = next((i for i in post.images if i.role == "featured"), None)

        payload = {
            "posts": [
                {
                    "title": post.title,
                    "slug": post.slug,
                    "html": render_html(post),
                    "custom_excerpt": (
                        post.seo.meta_description if post.seo else post.executive_summary
                    )[:300],
                    "status": "published" if settings.PUBLISH_STATUS == "publish" else "draft",
                    "meta_title": post.seo.seo_title if post.seo else post.title,
                    "meta_description": post.seo.meta_description if post.seo else "",
                    "feature_image": featured.public_url if featured else None,
                    "feature_image_alt": featured.alt_text if featured else None,
                    "tags": [{"name": k} for k in (post.seo.keywords[:5] if post.seo else [])],
                    "codeinjection_head": (
                        '<script type="application/ld+json">'
                        + json.dumps(post.seo.json_ld)
                        + "</script>"
                        if post.seo and post.seo.json_ld
                        else None
                    ),
                }
            ]
        }

        async with http_client() as client:
            async def _call():
                r = await client.post(
                    url, json=payload,
                    headers={"Authorization": f"Ghost {self._token()}"},
                )
                r.raise_for_status()
                return r.json()

            data = await with_retry(_call, label="publish:ghost")

        created = data["posts"][0]
        return PublishResult("ghost", created["id"], created.get("url"), created)


class MediumPublisher(Publisher):
    """Medium's write API is effectively frozen and tokens are no longer issued
    to new integrations. Kept for accounts that still hold a working token —
    treat as best-effort syndication with a canonical link home."""

    target = "medium"

    def is_configured(self) -> bool:
        return bool(settings.MEDIUM_INTEGRATION_TOKEN)

    async def publish(self, post: Post) -> PublishResult:
        headers = {
            "Authorization": f"Bearer {settings.MEDIUM_INTEGRATION_TOKEN}",
            "Content-Type": "application/json",
        }

        async with http_client() as client:
            author_id = settings.MEDIUM_AUTHOR_ID
            if not author_id:
                me = await client.get("https://api.medium.com/v1/me", headers=headers)
                me.raise_for_status()
                author_id = me.json()["data"]["id"]

            canonical = post.seo.canonical_url if post.seo else (
                f"{settings.SITE_BASE_URL.rstrip('/')}/blog/{post.slug}"
            )

            async def _call():
                r = await client.post(
                    f"https://api.medium.com/v1/users/{author_id}/posts",
                    json={
                        "title": post.title,
                        "contentFormat": "html",
                        "content": render_html(post),
                        "tags": (post.seo.keywords[:5] if post.seo else []),
                        # Canonical URL is essential: without it Medium
                        # competes with your own site for the same content.
                        "canonicalUrl": canonical,
                        "publishStatus": (
                            "public" if settings.PUBLISH_STATUS == "publish" else "draft"
                        ),
                        "notifyFollowers": True,
                    },
                    headers=headers,
                )
                r.raise_for_status()
                return r.json()

            data = await with_retry(_call, label="publish:medium")

        return PublishResult("medium", data["data"]["id"], data["data"]["url"], data)


class CustomCMSPublisher(Publisher):
    """Generic JSON webhook. Sends the complete post envelope so a bespoke CMS
    can map whatever it needs without a second round trip."""

    target = "custom"

    def is_configured(self) -> bool:
        return bool(settings.CUSTOM_CMS_URL)

    async def publish(self, post: Post) -> PublishResult:
        featured = next((i for i in post.images if i.role == "featured"), None)
        payload = {
            "title": post.title,
            "subtitle": post.subtitle,
            "slug": post.slug,
            "excerpt": post.executive_summary,
            "body_markdown": post.body_markdown,
            "body_html": render_html(post),
            "category": post.category,
            "status": settings.PUBLISH_STATUS,
            "reading_time_minutes": post.reading_time_minutes,
            "word_count": post.word_count,
            "highlights": post.highlights,
            "key_takeaways": post.key_takeaways,
            "citations": post.citations,
            "featured_image": (
                {"url": featured.public_url, "alt": featured.alt_text} if featured else None
            ),
            "seo": (
                {
                    "title": post.seo.seo_title,
                    "description": post.seo.meta_description,
                    "canonical_url": post.seo.canonical_url,
                    "keywords": post.seo.keywords,
                    "json_ld": post.seo.json_ld,
                    "og": post.seo.og_tags,
                    "twitter": post.seo.twitter_card,
                    "faq": post.seo.faq,
                }
                if post.seo
                else None
            ),
        }

        async with http_client() as client:
            async def _call():
                r = await client.post(
                    settings.CUSTOM_CMS_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.CUSTOM_CMS_TOKEN}"},
                )
                r.raise_for_status()
                return r.json() if r.content else {}

            data = await with_retry(_call, label="publish:custom")

        return PublishResult("custom", str(data.get("id", "")), data.get("url"), data)


class MarkdownPublisher(Publisher):
    """Writes a Hugo/Astro/Eleventy-compatible file with YAML front matter.

    This is the default target because it needs no credentials — you can run
    the whole pipeline end to end on a laptop and inspect real output before
    pointing it at a live CMS.
    """

    target = "markdown"

    async def publish(self, post: Post) -> PublishResult:
        directory = Path(settings.MARKDOWN_OUTPUT_DIR)
        directory.mkdir(parents=True, exist_ok=True)

        featured = next((i for i in post.images if i.role == "featured"), None)
        published = post.published_at or datetime.now(timezone.utc)

        front: dict = {
            "title": post.title,
            "subtitle": post.subtitle,
            "slug": post.slug,
            "date": published.isoformat(),
            "draft": settings.PUBLISH_STATUS != "publish",
            "category": post.category,
            "description": post.seo.meta_description if post.seo else post.executive_summary,
            "keywords": post.seo.keywords if post.seo else [],
            "readingTime": post.reading_time_minutes,
            "wordCount": post.word_count,
        }
        if featured and featured.public_url:
            front["image"] = featured.public_url
            front["imageAlt"] = featured.alt_text

        body = "\n".join(
            [
                "---",
                _yaml(front),
                "---",
                "",
                f"> {post.executive_summary}" if post.executive_summary else "",
                "",
                "## Highlights",
                *[f"- {h}" for h in (post.highlights or [])],
                "",
                post.body_markdown,
                "",
                "## Expert opinion",
                post.expert_opinion or "",
                "",
                "## Industry impact",
                post.industry_impact or "",
                "",
                "## What happens next",
                post.future_predictions or "",
                "",
                "## Key takeaways",
                *[f"- {t}" for t in (post.key_takeaways or [])],
                "",
                "## Sources",
                *[
                    f"- [{c['title']}]({c['url']}) — {c.get('publisher', '')}"
                    for c in (post.citations or [])
                    if c.get("url")
                ],
                "",
                "---",
                "",
                "*Researched and drafted with AI assistance from the sources above, "
                "and reviewed before publication.*",
            ]
        )

        path = directory / f"{published:%Y-%m-%d}-{post.slug}.md"
        path.write_text(body, encoding="utf-8")

        url = f"{settings.SITE_BASE_URL.rstrip('/')}/blog/{post.slug}"
        return PublishResult("markdown", str(path), url, {"path": str(path)})


def _yaml(data: dict) -> str:
    """Minimal YAML emitter — avoids a PyYAML dependency for 6 scalar types."""
    lines = []
    for key, value in data.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            rendered = ", ".join(json.dumps(str(v)) for v in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {json.dumps(str(value))}")
    return "\n".join(lines)


PUBLISHERS: dict[str, type[Publisher]] = {
    "wordpress": WordPressPublisher,
    "ghost": GhostPublisher,
    "medium": MediumPublisher,
    "custom": CustomCMSPublisher,
    "markdown": MarkdownPublisher,
}


def get_publishers() -> list[Publisher]:
    """Instantiate configured targets, skipping any missing credentials."""
    result: list[Publisher] = []
    for name in settings.publish_targets:
        cls = PUBLISHERS.get(name.strip())
        if not cls:
            log.warning("unknown_publish_target", target=name)
            continue
        publisher = cls()
        if not publisher.is_configured():
            log.warning("publish_target_not_configured", target=name)
            continue
        result.append(publisher)
    return result
