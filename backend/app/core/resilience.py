"""Retry, rate limiting and circuit breaking for outbound calls.

Three distinct failure modes are handled separately:
  * transient network/5xx  -> exponential backoff with jitter (tenacity)
  * provider rate limits   -> distributed token bucket in Redis, so N workers
                              share one budget instead of each assuming it owns
                              the full quota
  * persistently dead host -> circuit breaker, so one broken RSS feed does not
                              burn the whole collection window in timeouts
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx
import redis.asyncio as aioredis
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import settings
from app.core.logging_conf import get_logger

log = get_logger(__name__)
T = TypeVar("T")

_redis: aioredis.Redis | None = None


def redis_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(str(settings.REDIS_URL), decode_responses=True)
    return _redis


# --------------------------------------------------------------------- retry
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    # Provider SDKs expose `.status_code`; treat those the same way.
    status = getattr(exc, "status_code", None)
    return status in RETRYABLE_STATUS if isinstance(status, int) else False


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int | None = None,
    label: str = "call",
) -> T:
    """Run `fn` with exponential backoff + full jitter.

    Jitter matters: without it, 6 Celery workers that hit a 429 at the same
    moment all wake up at the same moment and hit it again.
    """
    attempts = attempts or settings.LLM_MAX_RETRIES
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=1, max=60, jitter=3),
        retry=retry_if_exception(is_retryable),
        reraise=True,
    ):
        with attempt:
            if attempt.retry_state.attempt_number > 1:
                log.warning(
                    "retrying", label=label, attempt=attempt.retry_state.attempt_number
                )
            return await fn()
    raise RuntimeError("unreachable")


# --------------------------------------------------------------- rate limit
_TOKEN_BUCKET_LUA = """
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])   -- tokens per second
local capacity = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])
if tokens == nil then tokens = capacity; ts = now end

tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)
return {allowed, tostring(tokens)}
"""


class RateLimiter:
    """Cluster-wide token bucket. One instance per upstream (per provider,
    per source domain), shared across every worker process via Redis."""

    def __init__(self, name: str, rate_per_sec: float, capacity: int | None = None):
        self.key = f"rl:{name}"
        self.rate = rate_per_sec
        self.capacity = capacity or max(1, int(rate_per_sec * 10))

    async def acquire(self, cost: float = 1.0, max_wait: float = 120.0) -> None:
        r = redis_client()
        deadline = time.monotonic() + max_wait
        script = r.register_script(_TOKEN_BUCKET_LUA)
        while True:
            allowed, _ = await script(
                keys=[self.key], args=[self.rate, self.capacity, time.time(), cost]
            )
            if int(allowed) == 1:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"rate limit wait exceeded for {self.key}")
            await asyncio.sleep(min(1.0 / max(self.rate, 0.1), 2.0))


# ----------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Redis-backed breaker keyed by host. After `threshold` consecutive
    failures the host is skipped for `cooldown` seconds."""

    def __init__(self, name: str, threshold: int = 5, cooldown: int = 900):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown

    @property
    def _key(self) -> str:
        return f"cb:{self.name}"

    async def is_open(self) -> bool:
        return await redis_client().exists(f"{self._key}:open") == 1

    async def record_success(self) -> None:
        await redis_client().delete(f"{self._key}:fails")

    async def record_failure(self) -> None:
        r = redis_client()
        fails = await r.incr(f"{self._key}:fails")
        await r.expire(f"{self._key}:fails", self.cooldown)
        if fails >= self.threshold:
            await r.setex(f"{self._key}:open", self.cooldown, "1")
            log.error("circuit_opened", target=self.name, failures=fails)


# ---------------------------------------------------------------- semaphore
_semaphores: dict[str, asyncio.Semaphore] = {}


def concurrency(name: str, limit: int) -> asyncio.Semaphore:
    """Per-process concurrency cap — the cheap complement to the Redis
    rate limiter, which handles the cross-process case."""
    if name not in _semaphores:
        _semaphores[name] = asyncio.Semaphore(limit)
    return _semaphores[name]


def http_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.REQUEST_TIMEOUT_SECONDS, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": settings.HTTP_USER_AGENT},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        **kwargs,
    )
