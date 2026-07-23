"""API v1. Read endpoints are open; every mutating endpoint requires X-API-Key."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.security import require_api_key
from app.db.models import (
    CostLedger,
    PipelineRun,
    Post,
    PostStatus,
    RawArticle,
    RunStatus,
    Source,
    SourceKind,
)
from app.db.session import get_session
from app.schemas.api import (
    CostBreakdown,
    CostReport,
    HealthResponse,
    PipelineStats,
    PostDetail,
    PostStatusUpdate,
    PostSummary,
    RunAccepted,
    RunSummary,
    SourceCreate,
    SourceOut,
    SourceUpdate,
    TriggerRunRequest,
)

router = APIRouter()
protected = [Depends(require_api_key)]


# ============================================================ health
health_router = APIRouter(tags=["health"])


@health_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness. Intentionally dependency-free so a DB blip does not cause
    Kubernetes to kill an otherwise healthy pod."""
    return HealthResponse(status="ok", version="1.0.0", environment=settings.ENV)


@health_router.get("/health/ready", response_model=HealthResponse)
async def readiness(db: AsyncSession = Depends(get_session)) -> HealthResponse:
    """Readiness. Checks the dependencies needed to serve traffic."""
    checks: dict = {}
    ok = True

    try:
        await db.execute(select(1))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        ok = False

    try:
        from app.core.resilience import redis_client

        await redis_client().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
        ok = False

    return HealthResponse(
        status="ok" if ok else "error",
        version="1.0.0",
        environment=settings.ENV,
        checks=checks,
    )


@health_router.get("/health/deep", response_model=HealthResponse)
async def deep_health(db: AsyncSession = Depends(get_session)) -> HealthResponse:
    """Business-level health: is the system actually publishing?

    A green liveness probe on a pipeline that has not published in 36 hours is
    a false negative — this is the check that should page someone.
    """
    from app.llm.factory import spend_last_24h
    from app.workers.tasks import pipeline_health

    checks = await pipeline_health()
    checks["spend_24h_usd"] = round(await spend_last_24h(), 4)
    checks["budget_limit_usd"] = settings.DAILY_COST_LIMIT_USD

    enabled_sources = await db.scalar(
        select(func.count(Source.id)).where(Source.enabled.is_(True))
    )
    checks["enabled_sources"] = enabled_sources or 0

    state = "ok"
    if checks["posts_published_24h"] == 0:
        state = "degraded"
    if checks["last_run_status"] == "failed" or (enabled_sources or 0) < 3:
        state = "degraded"
    if checks["spend_24h_usd"] >= settings.DAILY_COST_LIMIT_USD:
        state = "degraded"

    return HealthResponse(
        status=state, version="1.0.0", environment=settings.ENV, checks=checks
    )


# ============================================================== runs
runs_router = APIRouter(prefix="/runs", tags=["runs"])


@runs_router.post(
    "", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED,
    dependencies=protected,
)
async def trigger_run(payload: TriggerRunRequest) -> RunAccepted:
    """Queue a pipeline run. Returns immediately with a Celery task id — a run
    takes 5-20 minutes, far past any sane HTTP timeout."""
    from app.workers.tasks import run_pipeline

    task = run_pipeline.delay(trigger=payload.trigger, posts=payload.posts)
    return RunAccepted(
        task_id=task.id,
        message=f"pipeline queued (trigger={payload.trigger}); poll GET /runs/{{id}}",
    )


@runs_router.get("", response_model=list[RunSummary])
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_session),
) -> list[RunSummary]:
    stmt = select(PipelineRun).order_by(PipelineRun.created_at.desc())
    if status_filter:
        stmt = stmt.where(PipelineRun.status == RunStatus(status_filter))
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return [RunSummary.model_validate(r) for r in rows]


@runs_router.get("/{run_id}", response_model=RunSummary)
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> RunSummary:
    run = await db.get(PipelineRun, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return RunSummary.model_validate(run)


# ============================================================= posts
posts_router = APIRouter(prefix="/posts", tags=["posts"])


@posts_router.get("", response_model=list[PostSummary])
async def list_posts(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    category: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> list[PostSummary]:
    stmt = (
        select(Post)
        .order_by(Post.published_at.desc().nullslast(), Post.created_at.desc())
        .options(selectinload(Post.images))
    )
    if status_filter:
        stmt = stmt.where(Post.status == PostStatus(status_filter))
    if category:
        stmt = stmt.where(Post.category == category)

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    summaries = []
    for post in rows:
        summary = PostSummary.model_validate(post)
        featured = next(
            (i for i in post.images if i.role == "featured" and i.public_url), None
        )
        if featured:
            summary.featured_image = featured.public_url
            summary.featured_image_alt = featured.alt_text
        summaries.append(summary)
    return summaries


@posts_router.get("/{post_id}", response_model=PostDetail)
async def get_post(post_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> PostDetail:
    post = (
        await db.execute(
            select(Post)
            .where(Post.id == post_id)
            .options(
                selectinload(Post.seo),
                selectinload(Post.images),
                selectinload(Post.social_posts),
                selectinload(Post.publications),
            )
        )
    ).scalar_one_or_none()
    if not post:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")
    return PostDetail.model_validate(post)


@posts_router.patch("/{post_id}/status", response_model=PostSummary, dependencies=protected)
async def update_post_status(
    post_id: uuid.UUID,
    payload: PostStatusUpdate,
    db: AsyncSession = Depends(get_session),
) -> PostSummary:
    """Editorial approve/reject for HUMAN_REVIEW workflows."""
    post = await db.get(Post, post_id)
    if not post:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")

    post.status = PostStatus(payload.status)
    if payload.note:
        post.quality_notes = {**(post.quality_notes or {}), "editor_note": payload.note}
    await db.commit()
    await db.refresh(post)
    return PostSummary.model_validate(post)


@posts_router.post(
    "/{post_id}/publish", response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED, dependencies=protected,
)
async def publish_post(
    post_id: uuid.UUID, db: AsyncSession = Depends(get_session)
) -> RunAccepted:
    from app.workers.tasks import publish_post as publish_task

    post = await db.get(Post, post_id)
    if not post:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")
    if post.status == PostStatus.published:
        raise HTTPException(status.HTTP_409_CONFLICT, "post is already published")
    if post.status not in (PostStatus.approved, PostStatus.ready_for_review):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"post is {post.status.value}; approve it before publishing",
        )

    task = publish_task.delay(str(post_id))
    return RunAccepted(task_id=task.id, message="publish queued")


@posts_router.get("/{post_id}/preview", response_class=None)
async def preview_post(post_id: uuid.UUID, db: AsyncSession = Depends(get_session)):
    """Rendered HTML exactly as the CMS will receive it. Invaluable for
    eyeballing output before flipping PUBLISH_STATUS to `publish`."""
    from fastapi.responses import HTMLResponse

    from app.services.publishers.base import render_html

    post = (
        await db.execute(
            select(Post)
            .where(Post.id == post_id)
            .options(selectinload(Post.seo), selectinload(Post.images))
        )
    ).scalar_one_or_none()
    if not post:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "post not found")

    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{post.title}</title>"
        f"<style>body{{max-width:44rem;margin:3rem auto;padding:0 1rem;"
        f"font:16px/1.7 system-ui,sans-serif}}img{{max-width:100%;height:auto}}"
        f"pre{{overflow-x:auto;background:#f6f8fa;padding:1rem;border-radius:6px}}"
        f"</style></head><body><h1>{post.title}</h1>{render_html(post)}</body></html>"
    )


# =========================================================== sources
sources_router = APIRouter(prefix="/sources", tags=["sources"])


@sources_router.get("", response_model=list[SourceOut])
async def list_sources(
    enabled: bool | None = None, db: AsyncSession = Depends(get_session)
) -> list[SourceOut]:
    stmt = select(Source).order_by(Source.trust_score.desc())
    if enabled is not None:
        stmt = stmt.where(Source.enabled.is_(enabled))
    rows = (await db.execute(stmt)).scalars().all()
    return [SourceOut.model_validate(s) for s in rows]


@sources_router.post(
    "", response_model=SourceOut, status_code=status.HTTP_201_CREATED, dependencies=protected
)
async def create_source(
    payload: SourceCreate, db: AsyncSession = Depends(get_session)
) -> SourceOut:
    if await db.scalar(select(Source).where(Source.slug == payload.slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"slug '{payload.slug}' already exists")

    source = Source(
        slug=payload.slug,
        name=payload.name,
        kind=SourceKind(payload.kind),
        url=str(payload.url),
        categories=payload.categories,
        trust_score=payload.trust_score,
        enabled=payload.enabled,
        config=payload.config,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return SourceOut.model_validate(source)


@sources_router.patch("/{source_id}", response_model=SourceOut, dependencies=protected)
async def update_source(
    source_id: int, payload: SourceUpdate, db: AsyncSession = Depends(get_session)
) -> SourceOut:
    source = await db.get(Source, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(source, field, str(value) if field == "url" else value)
    await db.commit()
    await db.refresh(source)
    return SourceOut.model_validate(source)


@sources_router.delete(
    "/{source_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=protected
)
async def delete_source(source_id: int, db: AsyncSession = Depends(get_session)) -> None:
    source = await db.get(Source, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    await db.delete(source)
    await db.commit()


@sources_router.post("/seed", dependencies=protected)
async def seed_sources() -> dict:
    from app.workers.tasks import seed_sources as seed_task

    return {"task_id": seed_task.delay().id}


# ========================================================= analytics
analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])


@analytics_router.get("/costs", response_model=CostReport)
async def cost_report(
    days: int = Query(30, ge=1, le=365), db: AsyncSession = Depends(get_session)
) -> CostReport:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        await db.execute(select(CostLedger).where(CostLedger.day >= since))
    ).scalars().all()
    total = sum(float(r.cost_usd) for r in rows)

    posts = await db.scalar(
        select(func.count(Post.id)).where(
            Post.status == PostStatus.published, Post.published_at >= since
        )
    ) or 0

    return CostReport(
        period_days=days,
        total_cost_usd=round(total, 4),
        cost_per_post_usd=round(total / posts, 4) if posts else 0.0,
        posts_published=posts,
        projected_monthly_usd=round(total / days * 30, 2),
        budget_limit_usd=settings.DAILY_COST_LIMIT_USD * 30,
        breakdown=[
            CostBreakdown(
                provider=r.provider, model=r.model, category=r.category,
                requests=r.requests, input_tokens=r.input_tokens,
                output_tokens=r.output_tokens, cost_usd=round(float(r.cost_usd), 5),
            )
            for r in sorted(rows, key=lambda x: float(x.cost_usd), reverse=True)[:25]
        ],
    )


@analytics_router.get("/pipeline", response_model=PipelineStats)
async def pipeline_stats(
    days: int = Query(30, ge=1, le=365), db: AsyncSession = Depends(get_session)
) -> PipelineStats:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    runs = (
        await db.execute(select(PipelineRun).where(PipelineRun.created_at >= since))
    ).scalars().all()

    finished = [r for r in runs if r.started_at and r.finished_at]
    durations = [(r.finished_at - r.started_at).total_seconds() for r in finished]
    succeeded = sum(1 for r in runs if r.status == RunStatus.succeeded)
    failed = sum(1 for r in runs if r.status == RunStatus.failed)

    collected = sum(r.articles_collected for r in runs)
    after_dedupe = sum(r.articles_after_dedupe for r in runs)

    posts = await db.scalar(
        select(func.count(Post.id)).where(
            Post.status == PostStatus.published, Post.published_at >= since
        )
    ) or 0

    return PipelineStats(
        runs_total=len(runs),
        runs_succeeded=succeeded,
        runs_failed=failed,
        success_rate=round(succeeded / len(runs), 3) if runs else 0.0,
        avg_duration_seconds=round(sum(durations) / len(durations), 1) if durations else 0.0,
        avg_cost_per_run_usd=round(
            sum(float(r.total_cost_usd) for r in runs) / len(runs), 4
        ) if runs else 0.0,
        posts_published=posts,
        articles_collected=collected,
        dedupe_compression=round(1 - after_dedupe / collected, 3) if collected else 0.0,
    )


@analytics_router.get("/articles/top")
async def top_articles(
    limit: int = Query(20, ge=1, le=100),
    hours: int = Query(24, ge=1, le=168),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Current candidate pool with scores — useful for debugging why a
    particular story did or did not lead."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(RawArticle, Source.name)
            .join(Source, Source.id == RawArticle.source_id)
            .where(RawArticle.collected_at >= since, RawArticle.is_duplicate.is_(False))
            .order_by(RawArticle.social_score.desc(), RawArticle.published_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": str(a.id),
            "title": a.title,
            "url": a.url,
            "source": source_name,
            "categories": a.categories,
            "quality_score": a.quality_score,
            "social_score": a.social_score,
            "published_at": a.published_at.isoformat() if a.published_at else None,
        }
        for a, source_name in rows
    ]


for sub in (health_router, runs_router, posts_router, sources_router, analytics_router):
    router.include_router(sub)
