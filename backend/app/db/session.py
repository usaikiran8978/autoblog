"""Async engine/session plumbing.

Celery tasks are sync entry points that drive async code, so `run_async`
gives them a single, safe way to await a coroutine without leaking event
loops between tasks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Coroutine, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

T = TypeVar("T")

engine = create_async_engine(
    settings.sqlalchemy_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,      # survives Postgres restarts / idle connection reaps
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on clean exit, rolls back on exception."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Bridge for Celery's synchronous task functions."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a loop (e.g. eager mode in tests): use a private loop.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
