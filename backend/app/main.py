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
        publish_targets=settings.PUBLISH_TARGETS,
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
    allow_origins=settings.CORS_ORIGINS,
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
