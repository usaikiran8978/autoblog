"""FastAPI application entrypoint."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.v1.routes import router as v1_router
from app.config import settings
from app.core.logging_conf import configure_logging, get_logger

log = get_logger(__name__)


async def _auto_migrate() -> None:
    """Bring the database schema up to head at startup.

    Only used where no pre-deploy hook exists (see AUTO_MIGRATE). Alembic is
    synchronous and holds a transaction, so it runs in a worker thread to
    avoid blocking the event loop.

    Migration 0001 creates the `vector`, `pg_trgm` and `unaccent` extensions,
    so a bare database becomes fully usable here.

    A failure is logged but does not abort startup: the process still serves
    /health, which is what you need in order to diagnose it. Endpoints that
    touch the database will return 500 until it is resolved.
    """
    import asyncio
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    def _upgrade() -> None:
        # WORKDIR is /app; alembic.ini sits beside migrations/ and backend/.
        root = Path(__file__).resolve().parents[2]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        # Must be the psycopg-qualified URL: a bare postgresql:// resolves to
        # psycopg2, which is not installed.
        cfg.set_main_option("sqlalchemy.url", settings.sync_database_url)
        command.upgrade(cfg, "head")

    try:
        await asyncio.to_thread(_upgrade)
        log.info("auto_migrate_completed")
    except Exception as exc:
        log.error("auto_migrate_failed", error=str(exc), exc_info=True)


async def _auto_seed() -> None:
    """Populate the source registry if it is empty. Idempotent and cheap."""
    from sqlalchemy import func, select

    from app.db.models import Source
    from app.db.session import session_scope

    try:
        async with session_scope() as db:
            count = await db.scalar(select(func.count(Source.id)))
            if count:
                log.info("auto_seed_skipped", existing_sources=count)
                return

        from app.services.source_registry import SEED_SOURCES
        from app.db.models import SourceKind

        async with session_scope() as db:
            for entry in SEED_SOURCES:
                db.add(
                    Source(
                        slug=entry["slug"],
                        name=entry["name"],
                        kind=SourceKind(entry["kind"]),
                        url=entry["url"],
                        categories=entry["categories"],
                        trust_score=entry["trust"],
                        config=entry.get("config", {}),
                    )
                )
        log.info("auto_seed_completed", sources=len(SEED_SOURCES))
    except Exception as exc:
        log.error("auto_seed_failed", error=str(exc), exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info(
        "api_starting",
        env=settings.ENV,
        provider=settings.MODEL_PROVIDER,
        vector_backend=settings.VECTOR_BACKEND,
        schedule=settings.SCHEDULE,
        timezone=settings.TIMEZONE,
        publish_targets=settings.publish_targets,
    )

    if settings.SENTRY_DSN:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENV,
            traces_sample_rate=0.1,
            # Never ship prompt/article bodies to an error tracker.
            send_default_pii=False,
        )

    if settings.AUTO_MIGRATE:
        await _auto_migrate()

    if settings.AUTO_SEED:
        await _auto_seed()

    if settings.VECTOR_BACKEND == "qdrant":
        from app.services.vector import get_vector_store

        await get_vector_store().ensure_collection()  # type: ignore[union-attr]

    if settings.RUN_ON_STARTUP:
        from app.workers.tasks import run_pipeline

        run_pipeline.delay(trigger="manual")
        log.info("startup_run_queued")

    yield

    from app.db.session import engine

    await engine.dispose()
    log.info("api_stopped")


app = FastAPI(
    title="AutoBlog — AI Tech Blog Automation",
    description=(
        "Automated pipeline that collects tech news, deduplicates it, ranks it, "
        "writes original analysis, generates SEO metadata and imagery, publishes "
        "to a CMS, and produces social copy — twice a day."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENV != "prod" else None,
    redoc_url="/redoc" if settings.ENV != "prod" else None,
    openapi_url="/openapi.json" if settings.ENV != "prod" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Matched in addition to the exact list — covers PaaS-assigned hostname
    # suffixes that cannot be known when the deployment config is written.
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Response-Time-ms"] = str(duration_ms)

    # Health checks would otherwise dominate the log volume.
    if not request.url.path.startswith("/api/v1/health"):
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Never leak a stack trace to a client; always log the full one."""
    log.error("unhandled_exception", path=request.url.path, error=str(exc), exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(v1_router, prefix=settings.API_PREFIX)

# /metrics for Prometheus.
Instrumentator(
    should_group_status_codes=True,
    excluded_handlers=["/metrics", "/api/v1/health"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "autoblog",
        "version": "1.0.0",
        "docs": "/docs",
        "health": f"{settings.API_PREFIX}/health",
        "metrics": "/metrics",
    }
