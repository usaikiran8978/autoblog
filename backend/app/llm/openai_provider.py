"""OpenAI provider (chat completions + embeddings).

Note on model IDs: the brief asked for "GPT-5.5", which is not a model OpenAI
publishes. `OPENAI_MODEL_SMART` defaults to `gpt-5.1` and is env-driven, so
point it at whatever is current in your account without touching code — run
`GET /v1/models` to see what your key can actually reach.
"""

from __future__ import annotations

import json
import time

from openai import AsyncOpenAI

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import LLM_COST, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS
from app.core.resilience import RateLimiter, concurrency, with_retry
from app.llm.base import LLMProvider, LLMResponse, Tier, Usage
from app.llm.pricing import estimate_cost

log = get_logger(__name__)

EMBED_BATCH_SIZE = 96  # stays under per-request payload limits


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is required for the OpenAI provider")
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
        self.limiter = RateLimiter("openai", rate_per_sec=8.0, capacity=80)
        self.embed_limiter = RateLimiter("openai-embed", rate_per_sec=20.0, capacity=200)

    def model_for(self, tier: Tier) -> str:
        return settings.OPENAI_MODEL_SMART if tier == "smart" else settings.OPENAI_MODEL_FAST

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        tier: Tier = "smart",
        max_tokens: int = 16000,
        json_schema: dict | None = None,
        cache_system: bool = True,
        stream: bool | None = None,
    ) -> LLMResponse:
        model = self.model_for(tier)

        kwargs: dict = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": json_schema,
                },
            }

        await self.limiter.acquire()
        started = time.perf_counter()

        async def _call():
            async with concurrency("llm", settings.LLM_MAX_CONCURRENCY):
                return await self.client.chat.completions.create(**kwargs)

        try:
            resp = await with_retry(_call, label=f"openai:{model}")
        except Exception as exc:
            LLM_REQUESTS.labels("openai", model, "error").inc()
            log.error("llm_call_failed", provider="openai", model=model, error=str(exc))
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = resp.choices[0]
        text = choice.message.content or ""

        u = resp.usage
        cached = 0
        if u and getattr(u, "prompt_tokens_details", None):
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0

        usage = Usage(
            input_tokens=(u.prompt_tokens if u else 0) - cached,
            output_tokens=u.completion_tokens if u else 0,
            cache_read_tokens=cached,
        )
        usage.cost_usd = estimate_cost(
            "openai",
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
        _record(model, usage, latency_ms)

        parsed = None
        if json_schema and text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                log.warning("structured_output_parse_failed", model=model)

        return LLMResponse(
            text=text,
            parsed=parsed,
            model=model,
            provider="openai",
            usage=usage,
            stop_reason=choice.finish_reason,
            latency_ms=latency_ms,
            raw=resp,
        )

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        if not texts:
            return [], Usage()

        model = settings.EMBEDDING_MODEL
        vectors: list[list[float]] = []
        total = Usage()

        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            await self.embed_limiter.acquire()

            async def _call(b=batch):
                return await self.client.embeddings.create(model=model, input=b)

            resp = await with_retry(_call, label=f"embed:{model}")
            vectors.extend(item.embedding for item in resp.data)

            tokens = resp.usage.prompt_tokens if resp.usage else 0
            total.input_tokens += tokens
            total.cost_usd += estimate_cost("openai", model, input_tokens=tokens)

        LLM_TOKENS.labels("openai", model, "input").inc(total.input_tokens)
        LLM_COST.labels("openai", model).inc(total.cost_usd)
        return vectors, total


def _record(model: str, usage: Usage, latency_ms: int) -> None:
    LLM_REQUESTS.labels("openai", model, "ok").inc()
    LLM_TOKENS.labels("openai", model, "input").inc(usage.input_tokens)
    LLM_TOKENS.labels("openai", model, "output").inc(usage.output_tokens)
    LLM_COST.labels("openai", model).inc(usage.cost_usd)
    LLM_LATENCY.labels("openai", model).observe(latency_ms / 1000)
