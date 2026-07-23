"""Token pricing table, USD per 1M tokens.

Kept in code (not the DB) so cost estimates are versioned with the deploy that
produced them. Verify against the vendor pricing pages before relying on these
for billing — they are estimates for budgeting and alerting, not invoices.

Sources:
  Anthropic — https://platform.claude.com/docs/en/pricing
  OpenAI    — https://openai.com/api/pricing
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Price:
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


# ------------------------------------------------------------------ Anthropic
# Cache reads are ~0.1x input; 5-minute cache writes are ~1.25x input.
ANTHROPIC_PRICING: dict[str, Price] = {
    "claude-opus-4-8": Price(5.00, 25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-7": Price(5.00, 25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-6": Price(5.00, 25.00, cache_read=0.50, cache_write=6.25),
    "claude-sonnet-5": Price(3.00, 15.00, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-4-6": Price(3.00, 15.00, cache_read=0.30, cache_write=3.75),
    "claude-haiku-4-5": Price(1.00, 5.00, cache_read=0.10, cache_write=1.25),
}

# --------------------------------------------------------------------- OpenAI
OPENAI_PRICING: dict[str, Price] = {
    "gpt-5.1": Price(1.25, 10.00, cache_read=0.125),
    "gpt-5.1-mini": Price(0.25, 2.00, cache_read=0.025),
    "gpt-5": Price(1.25, 10.00, cache_read=0.125),
    "gpt-5-mini": Price(0.25, 2.00, cache_read=0.025),
    "gpt-4.1": Price(2.00, 8.00, cache_read=0.50),
    "gpt-4.1-mini": Price(0.40, 1.60, cache_read=0.10),
    "text-embedding-3-small": Price(0.02, 0.0),
    "text-embedding-3-large": Price(0.13, 0.0),
}

# Flat per-image cost. Coarse — providers price by size/quality — but good
# enough for the budget guard.
IMAGE_PRICING: dict[str, float] = {
    "gpt-image-1": 0.042,
    "dall-e-3": 0.040,
    "black-forest-labs/flux-1.1-pro": 0.040,
    "stability-sd3.5-large": 0.065,
}

_FALLBACK = Price(3.00, 15.00)


def price_for(provider: str, model: str) -> Price:
    table = ANTHROPIC_PRICING if provider == "claude" else OPENAI_PRICING
    if model in table:
        return table[model]
    # Tolerate dated snapshots like `gpt-5.1-2025-11-13`.
    for known, price in table.items():
        if model.startswith(known):
            return price
    return _FALLBACK


def estimate_cost(
    provider: str,
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    p = price_for(provider, model)
    total = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1_000_000
    return round(total, 6)


def image_cost(model: str) -> float:
    return IMAGE_PRICING.get(model, 0.05)
