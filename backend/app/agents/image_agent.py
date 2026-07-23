"""Agent 6 — Image Prompt Agent (+ generation).

Split in two on purpose: writing the prompt is a language task on the FAST
tier; rendering it is an image-API call. Keeping them separate means a
provider outage costs us the render, not the prompt, and prompts stay
reviewable/reusable.

Image generation is `optional=True`. A post with no hero image still
publishes; a pipeline that dies because an image API 503'd does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.base import Agent, AgentContext, record_cost
from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import IMAGES_GENERATED
from app.db.models import Post, PostImage
from app.llm.factory import get_provider
from app.prompts.templates import IMAGE_PROMPT_SCHEMA, IMAGE_PROMPT_SYSTEM
from app.services.images.providers import get_image_provider, store_image

log = get_logger(__name__)

# Appended to every prompt. Image models reliably add text unless told not to,
# repeatedly and explicitly.
STYLE_SUFFIX = (
    "Modern editorial illustration for a technology blog hero banner. "
    "Clean, uncluttered composition. Futuristic but grounded, not sci-fi kitsch. "
    "16:9 landscape. Generous negative space for a title overlay. "
    "Absolutely no text, no letters, no numbers, no words, no logos, "
    "no watermarks, no user interface elements, no signatures."
)

DEFAULT_NEGATIVE = (
    "text, letters, words, numbers, typography, captions, watermark, signature, "
    "logo, brand marks, UI elements, buttons, charts with labels, cluttered "
    "composition, low resolution, blurry, distorted anatomy, extra fingers, "
    "stock-photo people, corporate handshake, glowing blue brain cliché"
)


@dataclass(slots=True)
class ImageResult:
    prompt: str
    public_url: str | None
    alt_text: str
    generated: bool


class ImageAgent(Agent[str, ImageResult]):
    name = "image"
    optional = True

    async def execute(self, ctx: AgentContext, post_id: str) -> ImageResult:
        post = await ctx.db.get(Post, post_id)
        if not post:
            raise ValueError(f"post {post_id} not found")

        # ---- 1. write the prompt ---------------------------------------
        provider = get_provider()
        resp = await provider.complete(
            system=IMAGE_PROMPT_SYSTEM,
            prompt=(
                f"Article title: {post.title}\n"
                f"Subtitle: {post.subtitle}\n"
                f"Category: {post.category}\n\n"
                f"Summary: {post.executive_summary}\n\n"
                f"Key points:\n"
                + "\n".join(f"- {h}" for h in (post.highlights or [])[:4])
            ),
            tier="fast",
            max_tokens=2000,
            json_schema=IMAGE_PROMPT_SCHEMA,
        )
        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)

        data = resp.parsed or {}
        prompt = f"{data.get('prompt', post.title)}\n\n{STYLE_SUFFIX}"
        negative = data.get("negative_prompt") or DEFAULT_NEGATIVE
        alt_text = data.get("alt_text") or f"Illustration for: {post.title}"

        record = PostImage(
            post_id=post.id,
            role="featured",
            prompt=prompt,
            negative_prompt=negative,
            provider=settings.IMAGE_PROVIDER,
            alt_text=alt_text,
        )
        ctx.db.add(record)

        # ---- 2. render it ----------------------------------------------
        image_provider = get_image_provider()
        if image_provider is None:
            log.info("image_generation_disabled", post_id=post_id)
            await ctx.db.flush()
            return ImageResult(prompt, None, alt_text, generated=False)

        try:
            image = await image_provider.generate(prompt, negative)
        except Exception as exc:
            IMAGES_GENERATED.labels(settings.IMAGE_PROVIDER, "error").inc()
            log.error("image_generation_failed", post_id=post_id, error=str(exc))
            await ctx.db.flush()
            return ImageResult(prompt, None, alt_text, generated=False)

        storage_path, public_url = await store_image(image, post.slug)
        record.model = image.model
        record.storage_path = storage_path
        record.public_url = public_url
        record.width = image.width
        record.height = image.height
        record.bytes = len(image.data)
        record.cost_usd = image.cost_usd
        await ctx.db.flush()

        IMAGES_GENERATED.labels(image.provider, "ok").inc()
        log.info("image_generated", post_id=post_id, provider=image.provider,
                 url=public_url, cost_usd=image.cost_usd)
        return ImageResult(prompt, public_url, alt_text, generated=True)
