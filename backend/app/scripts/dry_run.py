"""Dry run: collect → dedupe → rank, then print the Top 10 and stop.

No writing, no images, no publishing. This is the cheapest way to sanity-check
source configuration and ranking weights — it costs a few cents in embeddings
and classification instead of a few dollars for a full run.

    make dry-run
"""

from __future__ import annotations

import asyncio
import uuid

from app.agents.base import AgentContext
from app.agents.collector import CollectorAgent
from app.agents.deduplicator import DeduplicatorAgent
from app.agents.ranker import RankingAgent
from app.core.logging_conf import configure_logging
from app.db.models import PipelineRun, RunStatus
from app.db.session import session_scope


async def main() -> None:
    configure_logging()

    async with session_scope() as db:
        run = PipelineRun(trigger="dry-run", status=RunStatus.running)
        db.add(run)
        await db.flush()
        ctx = AgentContext(run_id=run.id, db=db)

        collection = await CollectorAgent().run(ctx, None)
        print(f"\nCollected: {collection.collected} new articles")
        if collection.failed_sources:
            print(f"Failed sources: {', '.join(collection.failed_sources)}")

        dedupe = await DeduplicatorAgent().run(ctx, collection.article_ids)
        print(f"After dedupe: {len(dedupe.unique_ids)} unique "
              f"({dedupe.duplicates_removed} duplicates collapsed)")

        ranked = await RankingAgent().run(
            ctx, {"unique_ids": dedupe.unique_ids, "clusters": dedupe.clusters}
        )

        print(f"\n{'=' * 78}\nTOP {len(ranked)} STORIES\n{'=' * 78}")
        for i, story in enumerate(ranked, 1):
            print(f"\n{i}. [{story.score:.3f}] {story.title}")
            print(f"   {story.source}  |  cluster of {story.cluster_size}")
            print(f"   {story.url}")
            if story.angle:
                print(f"   Angle: {story.angle}")
            print("   " + "  ".join(f"{k}={v:.2f}" for k, v in story.components.items()))

        print(f"\n{'=' * 78}")
        print(f"Dry-run cost: ${ctx.usage.cost_usd:.4f} "
              f"({ctx.usage.input_tokens:,} in / {ctx.usage.output_tokens:,} out)")

        run.status = RunStatus.succeeded
        run.articles_collected = collection.collected
        run.articles_after_dedupe = len(dedupe.unique_ids)
        run.articles_ranked = len(ranked)


if __name__ == "__main__":
    asyncio.run(main())
