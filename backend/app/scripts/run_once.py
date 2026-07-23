"""Run one full pipeline cycle, then exit.

This is the entry point for environments with no always-on worker — chiefly
GitHub Actions on a cron schedule (see .github/workflows/publish.yml), which
is how the free-tier deployment publishes.

It calls the Coordinator directly, bypassing Celery entirely. Same agents,
same order, same guarantees; the only thing missing is the queue, which a
one-shot process does not need.

    python -m app.scripts.run_once            # publish
    python -m app.scripts.run_once --dry-run  # collect+dedupe+rank only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from zoneinfo import ZoneInfo

from app.config import settings
from app.core.logging_conf import configure_logging, get_logger

log = get_logger(__name__)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run one AutoBlog pipeline cycle")
    parser.add_argument("--posts", type=int, default=None, help="override POSTS_PER_RUN")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect, dedupe and rank only — no writing or publishing")
    parser.add_argument("--trigger", default="schedule", help="run label")
    args = parser.parse_args()

    configure_logging()

    if args.dry_run:
        from app.scripts.dry_run import main as dry_main

        await dry_main()
        return 0

    # Derive the slot the same way the Celery path does, so a GitHub Actions
    # run and a Celery run share one idempotency key and cannot double-publish
    # if you ever run both.
    now = datetime.now(ZoneInfo(settings.TIMEZONE))
    slot = "morning" if now.hour < 12 else "evening"
    key = f"pipeline:{now:%Y-%m-%d}:{slot}" if args.trigger == "schedule" else None

    from app.agents.coordinator import Coordinator

    result = await Coordinator().run(
        trigger=args.trigger,
        slot=slot,
        idempotency_key=key,
        posts_per_run=args.posts,
    )

    print("\n" + "=" * 72)
    print(f"run       {result.run_id}")
    print(f"status    {result.status}")
    print(f"collected {result.collected} → {result.after_dedupe} unique "
          f"→ {result.ranked} ranked")
    print(f"posts     {len(result.posts)}")
    print(f"cost      ${result.cost_usd:.4f}")
    print(f"duration  {result.duration_seconds:.0f}s")

    for post in result.posts:
        published = "published" if post["published"] else "held for review"
        print(f"\n  · {post['title']}")
        print(f"    {post['word_count']} words · originality "
              f"{post['originality']:.2f} · {published}")
        for warning in post.get("warnings", []):
            print(f"    ! {warning}")

    if result.warnings:
        print("\nwarnings:")
        for warning in result.warnings:
            print(f"  ! {warning}")
    if result.error:
        print(f"\nerror: {result.error}")
    print("=" * 72)

    # `duplicate` means the slot already ran — that is success, not failure.
    if result.status in ("succeeded", "partial", "duplicate"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
