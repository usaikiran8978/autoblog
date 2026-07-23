"""Publisher contract + shared markdown→HTML rendering."""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass

from markdown_it import MarkdownIt

from app.db.models import Post


@dataclass(slots=True)
class PublishResult:
    target: str
    external_id: str | None
    external_url: str | None
    raw: dict


class Publisher(abc.ABC):
    target: str

    @abc.abstractmethod
    async def publish(self, post: Post) -> PublishResult:
        ...

    def is_configured(self) -> bool:
        return True


_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
_md.enable(["table", "strikethrough"])


def render_html(post: Post) -> str:
    """Full post body as HTML, including the SEO extras the CMS will not build
    for us (JSON-LD, FAQ section, key takeaways)."""
    parts: list[str] = []

    if post.seo and post.seo.json_ld:
        parts.append(
            '<script type="application/ld+json">'
            + json.dumps(post.seo.json_ld, ensure_ascii=False)
            + "</script>"
        )

    featured = next((i for i in post.images if i.role == "featured" and i.public_url), None)
    if featured:
        parts.append(
            f'<figure class="post-hero">'
            f'<img src="{featured.public_url}" alt="{_esc(featured.alt_text or post.title)}" '
            f'width="{featured.width or 1536}" height="{featured.height or 1024}" '
            f'loading="eager" decoding="async">'
            f"</figure>"
        )

    if post.subtitle:
        parts.append(f'<p class="post-subtitle"><em>{_esc(post.subtitle)}</em></p>')

    if post.executive_summary:
        parts.append(
            f'<div class="post-summary"><strong>The short version:</strong> '
            f"{_esc(post.executive_summary)}</div>"
        )

    if post.highlights:
        items = "".join(f"<li>{_esc(h)}</li>" for h in post.highlights)
        parts.append(f'<aside class="post-highlights"><h2>Highlights</h2><ul>{items}</ul></aside>')

    parts.append(_md.render(post.body_markdown))

    for heading, body in (
        ("Expert opinion", post.expert_opinion),
        ("Industry impact", post.industry_impact),
        ("What happens next", post.future_predictions),
    ):
        if body:
            parts.append(f"<h2>{heading}</h2>{_md.render(body)}")

    if post.key_takeaways:
        items = "".join(f"<li>{_esc(t)}</li>" for t in post.key_takeaways)
        parts.append(f'<section class="post-takeaways"><h2>Key takeaways</h2><ul>{items}</ul></section>')

    if post.seo and post.seo.faq:
        faq_html = "".join(
            f"<details><summary>{_esc(q['question'])}</summary>"
            f"<p>{_esc(q['answer'])}</p></details>"
            for q in post.seo.faq
        )
        parts.append(f'<section class="post-faq"><h2>Frequently asked questions</h2>{faq_html}</section>')

    if post.citations:
        links = "".join(
            f'<li><a href="{_esc(c["url"])}" rel="nofollow noopener" target="_blank">'
            f'{_esc(c["title"])}</a> — {_esc(c.get("publisher", ""))}</li>'
            for c in post.citations
            if c.get("url")
        )
        parts.append(f'<section class="post-sources"><h2>Sources</h2><ul>{links}</ul></section>')

    # Transparency notice. Required by Google's guidance on AI-generated
    # content and simply the right thing to tell readers.
    parts.append(
        '<p class="ai-disclosure"><small>This article was researched and drafted '
        "with AI assistance from the sources listed above, and reviewed before "
        "publication.</small></p>"
    )
    return "\n".join(parts)


def _esc(text: str | None) -> str:
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
