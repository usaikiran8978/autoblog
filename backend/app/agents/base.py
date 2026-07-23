"""Agent base class.

Every agent is a class with one `run()` method, a typed input and a typed
output. The base class owns the cross-cutting concerns so no agent has to
remember them: timing, structured logging, Prometheus metrics, cost
accounting, and persistence of an `agent_runs` row.

That last part is the important one — it means every token spent anywhere in
the pipeline is attributable to a specific agent in a specific run, which is
what makes the cost dashboard possible.
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_conf import agent_ctx, get_logger
from app.core.metrics import STAGE_DURATION, STAGE_FAILURES
from app.db.models import AgentRun, CostLedger, PipelineRun, RunStatus
from app.llm.base import Usage

log = get_logger(__name__)

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


@dataclass
class AgentContext:
    """Threaded through every agent in a run. The one shared mutable object."""

    run_id: uuid.UUID
    db: AsyncSession
    usage: Usage = field(default_factory=Usage)
    state: dict[str, Any] = field(default_factory=dict)

    def add_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage


class AgentError(RuntimeError):
    """Agent failed in a way the coordinator should decide how to handle."""

    def __init__(self, agent: str, message: str, *, recoverable: bool = True):
        super().__init__(f"[{agent}] {message}")
        self.agent = agent
        self.recoverable = recoverable


class Agent(Generic[TIn, TOut], abc.ABC):
    name: str
    # Set False for agents whose failure should abort the run (writer, collector).
    optional: bool = False

    @abc.abstractmethod
    async def execute(self, ctx: AgentContext, payload: TIn) -> TOut:
        """The actual work. Subclasses implement this, callers use run()."""

    async def run(self, ctx: AgentContext, payload: TIn) -> TOut:
        token = agent_ctx.set(self.name)
        started = time.perf_counter()
        usage_before = ctx.usage
        record = AgentRun(
            run_id=ctx.run_id, agent=self.name, status=RunStatus.running
        )
        ctx.db.add(record)
        await ctx.db.flush()

        try:
            result = await self.execute(ctx, payload)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            record.status = RunStatus.failed
            record.error = f"{type(exc).__name__}: {exc}"[:2000]
            record.duration_ms = duration_ms
            await ctx.db.flush()

            STAGE_FAILURES.labels(self.name, type(exc).__name__).inc()
            STAGE_DURATION.labels(self.name).observe(duration_ms / 1000)
            log.error("agent_failed", agent=self.name, duration_ms=duration_ms,
                      error=str(exc), exc_info=True)
            raise AgentError(self.name, str(exc), recoverable=self.optional) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        delta = _usage_delta(usage_before, ctx.usage)

        record.status = RunStatus.succeeded
        record.duration_ms = duration_ms
        record.input_tokens = delta.input_tokens
        record.output_tokens = delta.output_tokens
        record.cache_read_tokens = delta.cache_read_tokens
        record.cache_write_tokens = delta.cache_write_tokens
        record.cost_usd = delta.cost_usd
        await ctx.db.flush()

        STAGE_DURATION.labels(self.name).observe(duration_ms / 1000)
        log.info(
            "agent_completed",
            agent=self.name,
            duration_ms=duration_ms,
            cost_usd=round(delta.cost_usd, 5),
            tokens_in=delta.input_tokens,
            tokens_out=delta.output_tokens,
        )
        agent_ctx.reset(token)
        return result


def _usage_delta(before: Usage, after: Usage) -> Usage:
    return Usage(
        after.input_tokens - before.input_tokens,
        after.output_tokens - before.output_tokens,
        after.cache_read_tokens - before.cache_read_tokens,
        after.cache_write_tokens - before.cache_write_tokens,
        round(after.cost_usd - before.cost_usd, 6),
    )


async def record_cost(
    db: AsyncSession, provider: str, model: str, category: str, usage: Usage
) -> None:
    """Upsert into the daily cost ledger. Feeds the budget guard and the
    /analytics/costs endpoint."""
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        pg_insert(CostLedger)
        .values(
            day=day,
            provider=provider,
            model=model,
            category=category,
            requests=1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
        .on_conflict_do_update(
            constraint="uq_cost_day_model",
            set_={
                "requests": CostLedger.requests + 1,
                "input_tokens": CostLedger.input_tokens + usage.input_tokens,
                "output_tokens": CostLedger.output_tokens + usage.output_tokens,
                "cost_usd": CostLedger.cost_usd + usage.cost_usd,
            },
        )
    )
    await db.execute(stmt)


async def finalize_run(db: AsyncSession, run_id: uuid.UUID) -> None:
    """Roll per-agent usage up onto the pipeline_runs row."""
    run = await db.get(PipelineRun, run_id)
    if not run:
        return
    rows = (
        await db.execute(select(AgentRun).where(AgentRun.run_id == run_id))
    ).scalars().all()
    run.total_input_tokens = sum(r.input_tokens for r in rows)
    run.total_output_tokens = sum(r.output_tokens for r in rows)
    run.total_cost_usd = sum(float(r.cost_usd) for r in rows)
    run.stage_timings = {r.agent: r.duration_ms for r in rows}
