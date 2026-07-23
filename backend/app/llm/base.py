"""Provider-agnostic LLM interface.

Every agent talks to this, never to a vendor SDK directly. That is what makes
MODEL_PROVIDER=openai|claude a one-line switch instead of a refactor.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal

Tier = Literal["smart", "fast"]


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            round(self.cost_usd + other.cost_usd, 6),
        )


@dataclass(slots=True)
class LLMResponse:
    text: str
    parsed: dict | list | None = None
    model: str = ""
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    latency_ms: int = 0
    raw: Any = None


class LLMProvider(abc.ABC):
    name: str

    @abc.abstractmethod
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
        """Single-turn completion.

        `json_schema` requests structured output; providers enforce it natively
        so callers never have to regex JSON out of prose.

        `cache_system` marks the system prompt as a cacheable prefix. Our system
        prompts are large and byte-identical across a run, which is exactly the
        shape prompt caching rewards.
        """

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> tuple[list[list[float]], Usage]:
        """Batch embedding. Callers always batch — one request per text is the
        most common way to accidentally 10x the embedding bill."""

    @abc.abstractmethod
    def model_for(self, tier: Tier) -> str:
        ...
