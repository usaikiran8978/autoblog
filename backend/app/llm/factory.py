"""Provider selection + the budget guard that wraps every call."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import select

from app.config import settings
from app.core.logging_conf import get_logger
from app.core.metrics import DAILY_SPEND
from app.db.models import CostLedger
from app.db.session import session_scope
from app.llm.base import LLMProvider

log = get_logger(__name__)


class BudgetExceeded(RuntimeError):
    """Raised when rolling 24h spend crosses DAILY_COST_LIMIT_USD."""


@lru_cache
def get_provider(name: str | None = None) -> LLMProvider:
    provider = (name or settings.MODEL_PROVIDER).lower()
    if provider == "claude":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if provider == "openai":
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider()
    raise ValueError(f"unknown MODEL_PROVIDER: {provider!r} (expected 'openai' or 'claude')")


@lru_cache
def get_embedding_provider() -> LLMProvider:
    """Embeddings are always OpenAI unless a local model is configured —
    Anthropic does not expose an embeddings endpoint."""
    from app.llm.openai_provider import OpenAIProvider

    return OpenAIProvider()


async def spend_last_24h() -> float:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    async with session_scope() as db:
        rows = (await db.execute(select(CostLedger).where(CostLedger.day >= since))).scalars()
        total = float(sum(float(r.cost_usd) for r in rows))
    DAILY_SPEND.set(total)
    return total


async def assert_within_budget() -> None:
    """Called before each expensive stage. Fails the run loudly rather than
    letting a retry loop quietly spend the month's budget overnight."""
    spent = await spend_last_24h()
    if spent >= settings.DAILY_COST_LIMIT_USD:
        raise BudgetExceeded(
            f"24h spend ${spent:.2f} >= limit ${settings.DAILY_COST_LIMIT_USD:.2f}"
        )
    if spent >= settings.DAILY_COST_LIMIT_USD * 0.8:
        log.warning("budget_warning", spent_usd=round(spent, 2),
                    limit_usd=settings.DAILY_COST_LIMIT_USD)
