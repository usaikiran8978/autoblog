"""Image generation backends behind one interface.

All four return the same `GeneratedImage`, so switching IMAGE_PROVIDER never
touches the agent. Storage is pluggable too (local disk or S3), because the
CMS upload step needs bytes either way.
"""

from __future__ import annotations

import abc
import base64
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import IMAGES_GENERATED
from app.core.resilience import http_client, with_retry
from app.llm.pricing import image_cost

log = get_logger(__name__)


@dataclass(slots=True)
class GeneratedImage:
    data: bytes
    mime_type: str
    provider: str
    model: str
    width: int
    height: int
    cost_usd: float


class ImageProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def generate(self, prompt: str, negative_prompt: str = "") -> GeneratedImage:
        ...


class OpenAIImageProvider(ImageProvider):
    name = "openai"

    async def generate(self, prompt: str, negative_prompt: str = "") -> GeneratedImage:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0)
        model = settings.OPENAI_IMAGE_MODEL

        # gpt-image-1 has no negative_prompt parameter; fold it into the prompt.
        full = f"{prompt}\n\nStrictly avoid: {negative_prompt}" if negative_prompt else prompt

        async def _call():
            return await client.images.generate(
                model=model, prompt=full, size=settings.IMAGE_SIZE, n=1
            )

        resp = await with_retry(_call, label="image:openai")
        b64 = resp.data[0].b64_json
        if not b64:
            # dall-e-3 returns a URL rather than base64.
            async with http_client() as http:
                data = (await http.get(resp.data[0].url)).content
        else:
            data = base64.b64decode(b64)

        width, height = (int(x) for x in settings.IMAGE_SIZE.split("x"))
        return GeneratedImage(data, "image/png", "openai", model, width, height,
                              image_cost(model))


class FluxProvider(ImageProvider):
    """Flux via Replicate. Best price/quality for editorial illustration."""

    name = "flux"

    async def generate(self, prompt: str, negative_prompt: str = "") -> GeneratedImage:
        if not settings.REPLICATE_API_TOKEN:
            raise RuntimeError("REPLICATE_API_TOKEN required for IMAGE_PROVIDER=flux")

        headers = {
            "Authorization": f"Bearer {settings.REPLICATE_API_TOKEN}",
            "Prefer": "wait",  # synchronous response, no polling loop
        }
        payload = {
            "input": {
                "prompt": prompt,
                "aspect_ratio": "16:9",
                "output_format": "png",
                "safety_tolerance": 2,
                **({"negative_prompt": negative_prompt} if negative_prompt else {}),
            }
        }

        async with http_client(timeout=httpx.Timeout(180.0)) as client:
            async def _call():
                r = await client.post(
                    f"https://api.replicate.com/v1/models/{settings.FLUX_MODEL}/predictions",
                    json=payload, headers=headers,
                )
                r.raise_for_status()
                return r.json()

            result = await with_retry(_call, label="image:flux")
            output = result.get("output")
            url = output[0] if isinstance(output, list) else output
            if not url:
                raise RuntimeError(f"flux returned no output: {result.get('error')}")
            data = (await client.get(url)).content

        return GeneratedImage(data, "image/png", "flux", settings.FLUX_MODEL,
                              1344, 768, image_cost(settings.FLUX_MODEL))


class StabilityProvider(ImageProvider):
    name = "stability"

    async def generate(self, prompt: str, negative_prompt: str = "") -> GeneratedImage:
        if not settings.STABILITY_API_KEY:
            raise RuntimeError("STABILITY_API_KEY required for IMAGE_PROVIDER=stability")

        async with http_client(timeout=httpx.Timeout(180.0)) as client:
            async def _call():
                r = await client.post(
                    "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                    headers={
                        "Authorization": f"Bearer {settings.STABILITY_API_KEY}",
                        "Accept": "image/*",
                    },
                    files={"none": ""},
                    data={
                        "prompt": prompt,
                        "negative_prompt": negative_prompt,
                        "aspect_ratio": "16:9",
                        "output_format": "png",
                        "model": "sd3.5-large",
                    },
                )
                r.raise_for_status()
                return r

            resp = await with_retry(_call, label="image:stability")

        return GeneratedImage(resp.content, "image/png", "stability", "sd3.5-large",
                              1344, 768, image_cost("stability-sd3.5-large"))


def get_image_provider() -> ImageProvider | None:
    return {
        "openai": OpenAIImageProvider,
        "flux": FluxProvider,
        "stability": StabilityProvider,
    }.get(settings.IMAGE_PROVIDER, lambda: None)() if settings.IMAGE_PROVIDER != "none" else None


# ---------------------------------------------------------------- storage
async def store_image(image: GeneratedImage, slug: str) -> tuple[str, str]:
    """Persist bytes, return (storage_path, public_url)."""
    filename = f"{slug}-{uuid.uuid4().hex[:8]}.png"

    if settings.IMAGE_STORAGE == "s3":
        import boto3

        s3 = boto3.client("s3", region_name=settings.S3_REGION)
        key = f"blog/featured/{filename}"
        s3.put_object(
            Bucket=settings.S3_BUCKET,
            Key=key,
            Body=image.data,
            ContentType=image.mime_type,
            CacheControl="public, max-age=31536000, immutable",
        )
        base = settings.S3_PUBLIC_BASE_URL or (
            f"https://{settings.S3_BUCKET}.s3.{settings.S3_REGION}.amazonaws.com"
        )
        return key, f"{base.rstrip('/')}/{key}"

    directory = Path(settings.IMAGE_LOCAL_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(image.data)
    public = f"{settings.SITE_BASE_URL.rstrip('/')}/media/{filename}"
    IMAGES_GENERATED.labels(image.provider, "stored").inc()
    return str(path), public
