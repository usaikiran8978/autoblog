"""Celery application + beat schedule.

Queue split matters: the `pipeline` queue holds long-running publish jobs
(10-20 min each) while `maintenance` holds short periodic tasks. Without the
split, a 20-minute writer call blocks health checks and analytics pulls behind
it in the same queue.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging, worker_process_init

from app.config import settings
from app.core.logging_conf import configure_logging

celery_app = Celery("autoblog", broker=settings.broker_url, backend=settings.result_backend)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.TIMEZONE,
    enable_utc=True,

    # A writer call can legitimately run for minutes. Hard limit is generous;
    # soft limit fires first so the task can clean up and mark the run failed.
    task_soft_time_limit=45 * 60,
    task_time_limit=50 * 60,

    # Long tasks + late ack means a worker crash re-queues the job instead of
    # silently losing an edition.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,   # never let one worker hoard pipeline jobs

    result_expires=7 * 24 * 3600,
    broker_connection_retry_on_startup=True,
    task_default_queue="pipeline",
    task_routes={
        "app.workers.tasks.run_pipeline": {"queue": "pipeline"},
        "app.workers.tasks.publish_post": {"queue": "pipeline"},
        "app.workers.tasks.*": {"queue": "maintenance"},
    },
)


def _beat_schedule() -> dict:
    """Parse SCHEDULE (crontab syntax) into beat entries.

    Supports the common `0 9,18 * * *` form directly so operators can change
    publish times with one env var and a restart.
    """
    try:
        minute, hour, dom, month, dow = settings.SCHEDULE.split()
    except ValueError as exc:
        raise ValueError(
            f"SCHEDULE must be 5-field crontab syntax, got {settings.SCHEDULE!r}"
        ) from exc

    return {
        "publish-cycle": {
            "task": "app.workers.tasks.run_pipeline",
            "schedule": crontab(
                minute=minute, hour=hour, day_of_month=dom,
                month_of_year=month, day_of_week=dow,
            ),
            "kwargs": {"trigger": "schedule"},
            # If beat and worker both restart around the fire time, don't run
            # an edition an hour late.
            "options": {"expires": 30 * 60},
        },
        "refresh-source-health": {
            "task": "app.workers.tasks.refresh_sources",
            "schedule": crontab(minute=0, hour="*/6"),
        },
        "collect-analytics": {
            "task": "app.workers.tasks.pull_analytics",
            "schedule": crontab(minute=30, hour=3),
        },
        "retry-failed-publications": {
            "task": "app.workers.tasks.retry_failed_publications",
            "schedule": crontab(minute="*/20"),
        },
        "prune-old-data": {
            "task": "app.workers.tasks.prune",
            "schedule": crontab(minute=0, hour=4, day_of_week=0),
        },
        "export-queue-depth": {
            "task": "app.workers.tasks.export_queue_depth",
            "schedule": 60.0,
        },
    }


celery_app.conf.beat_schedule = _beat_schedule()


@setup_logging.connect
def _configure_celery_logging(**_kwargs):
    configure_logging()
    return True


@worker_process_init.connect
def _init_worker(**_kwargs):
    """Each forked worker needs its own DB engine — connections cannot be
    shared across a fork() boundary."""
    from app.db.session import engine

    engine.dispose()


import app.workers.tasks  # noqa: E402,F401  (registers tasks on import)
