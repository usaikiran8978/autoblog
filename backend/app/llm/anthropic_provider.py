"""Claude provider.

Notes on API choices, since several of these changed recently:

* Adaptive thinking (`thinking={"type": "adaptive"}`) replaces the old
  `budget_tokens` form, which is rejected with a 400 on Opus 4.7+.
* Depth is controlled with `output_config.effort`, not a token budget.
* We stream whenever `max_tokens` is large. Non-streaming requests at high
  `max_tokens` hit SDK HTTP timeouts; `get_final_message()` gives us the
  whole message anyway, so streaming costs us nothing in code complexity.
* Assistant prefill is NOT supported on this model family (400). Structured
  output goes through `output_config.format` instead.
* The system prompt is marked with `cache_control` — it is large, byte-stable
  across a run, and read many times, which is exactly the caching sweet spot.
"""

from __future__ import annotations

import json
import time

from anthropic import AsyncAnthropic

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import LLM_COST, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS
from app.core.resilience import RateLimiter, concurrency, with_retry
from app.llm.base import LLMProvider, LLMResponse, Tier, Usage
from app.llm.pricing import estimate_cost

log = get_logger(__name__)

# Streaming threshold: below this a plain request is fine and slightly simpler.
_STREAM_ABOVE_TOKENS = 8000


class AnthropicProvider(LLMProvider):
    name = "claude"

    def __init__(self) -> None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is required when MODEL_PROVIDER=claude")
        self.client = AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=0,  # we own retry policy in core.resilience
        )
        self.limiter = RateLimiter("anthropic", rate_per_sec=4.0, capacity=40)

    def model_for(self, tier: Tier) -> str:
        return (
            settings.ANTHROPIC_MODEL_SMART if tier == "smart" else settings.ANTHROPIC_MODEL_FAST
        )

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
        should_stream = stream if stream is not None else max_tokens > _STREAM_ABOVE_TOKENS

        system_blocks: list[dict] = [{"type": "text", "text": system}]
        if cache_system and len(system) > 2000:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": prompt}],
            # Adaptive thinking must be requested explicitly — omitting the
            # field runs without thinking on this model family.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": settings.ANTHROPIC_EFFORT},
        }
        if json_schema:
            kwargs["output_config"]["format"] = {
                "type": "json_schema",
                "schema": json_schema,
            }

        await self.limiter.acquire()
        started = time.perf_counter()

        async def _call():
            async with concurrency("llm", settings.LLM_MAX_CONCURRENCY):
                if should_stream:
                    async with self.client.messages.stream(**kwargs) as s:
                        return await s.get_final_message()
                return await self.client.messages.create(**kwargs)

        try:
            msg = await with_retry(_call, label=f"anthropic:{model}")
        except Exception as exc:
            LLM_REQUESTS.labels("claude", model, "error").inc()
            log.error("llm_call_failed", provider="claude", model=model, error=str(exc))
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)

        # Guard stop_reason before reading content: a safety refusal returns
        # HTTP 200 with an empty content array.
        if msg.stop_reason == "refusal":
            LLM_REQUESTS.labels("claude", model, "refusal").inc()
            raise RuntimeError(f"model declined the request (stop_reason=refusal, model={model})")

        text = "".join(b.text for b in msg.content if b.type == "text")

        u = msg.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        usage.cost_usd = estimate_cost(
            "claude",
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
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
            provider="claude",
            usage=usage,
            stop_reason=msg.stop_reason,
            latency_ms=latency_ms,
            raw=msg,
        )

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        """Anthropic has no embeddings endpoint, so embeddings always route to
        the configured embedding provider. Kept here so callers never branch."""
        from app.llm.openai_provider import OpenAIProvider

        return await OpenAIProvider().embed(texts)


def _record(model: str, usage: Usage, latency_ms: int) -> None:
    LLM_REQUESTS.labels("claude", model, "ok").inc()
    LLM_TOKENS.labels("claude", model, "input").inc(usage.input_tokens)
    LLM_TOKENS.labels("claude", model, "output").inc(usage.output_tokens)
    LLM_TOKENS.labels("claude", model, "cache_read").inc(usage.cache_read_tokens)
    LLM_COST.labels("claude", model).inc(usage.cost_usd)
    LLM_LATENCY.labels("claude", model).observe(latency_ms / 1000)
