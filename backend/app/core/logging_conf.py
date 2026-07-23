"""Structured JSON logging. Every log line carries run_id/agent when available,
so a single pipeline execution can be traced end to end in Loki/CloudWatch."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

from app.config import settings

# Set once at the top of a pipeline run; picked up by every log line after.
run_id_ctx: ContextVar[str | None] = ContextVar("run_id", default=None)
agent_ctx: ContextVar[str | None] = ContextVar("agent", default=None)


def _inject_context(_logger, _method, event_dict):
    if rid := run_id_ctx.get():
        event_dict.setdefault("run_id", rid)
    if agent := agent_ctx.get():
        event_dict.setdefault("agent", agent)
    return event_dict


def configure_logging() -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.LOG_LEVEL.upper()
    )
    renderer = (
        structlog.dev.ConsoleRenderer()
        if settings.ENV == "dev"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    # Third-party noise floor.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "autoblog"):
    return structlog.get_logger(name)
