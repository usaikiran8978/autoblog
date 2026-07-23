"""Agent 3 — Ranking Agent.

Hybrid scoring. The deterministic half (recency, source authority, social
engagement, category fit) is arithmetic in Python — it is cheap, explainable,
and stable across runs. The subjective half (importance, novelty, depth,
audience fit, and the editorial angle) is the one thing an LLM is genuinely
better at, so only that part goes to the model.

Doing it this way means a ranking is auditable: every Top-10 entry carries the
component scores that produced it, so "why did this lead?" has an answer.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from app.agents.base import Agent, AgentContext, record_cost
from app.config import settings
from app.core.logging_conf import get_logger
from app.db.models import RawArticle, Source
from app.llm.factory import get_provider
from app.prompts.templates import RANKER_SCHEMA, RANKER_SYSTEM

log = get_logger(__name__)

# Deterministic vs. editorial weighting. Tuned so a genuinely important story
# from a trusted source still wins over a merely fresh one.
WEIGHTS = {
    "recency": 0.20,
    "authority": 0.15,
    "social": 0.15,
    "quality": 0.10,
    "importance": 0.20,
    "novelty": 0.10,
    "depth": 0.10,
}

# Only this many candidates go to the LLM. Ranking 400 items would cost more
# than writing the article.
LLM_CANDIDATES = 40


@dataclass
class RankedStory:
    article_id: str
    title: str
    url: str
    source: str
    score: float
    angle: str = ""
    components: dict = field(default_factory=dict)
    cluster_size: int = 1
    supporting_ids: list[str] = field(default_factory=list)


class RankingAgent(Agent[dict, list[RankedStory]]):
    name = "ranker"
    optional = False

    async def execute(self, ctx: AgentContext, payload: dict) -> list[RankedStory]:
        unique_ids = payload["unique_ids"]
        clusters: dict[str, list[str]] = payload.get("clusters", {})
        if not unique_ids:
            return []

        rows = (
            await ctx.db.execute(
                select(RawArticle, Source)
                .join(Source, Source.id == RawArticle.source_id)
                .where(RawArticle.id.in_(unique_ids))
            )
        ).all()

        # ---- deterministic pre-score -----------------------------------
        prescored: list[tuple[RawArticle, Source, dict]] = []
        for article, source in rows:
            components = {
                "recency": _recency(article.published_at or article.collected_at),
                "authority": float(source.trust_score),
                "social": article.social_score,
                "quality": article.quality_score if article.quality_score is not None else 0.5,
            }
            # Press releases are demoted hard rather than dropped — occasionally
            # the PR *is* the news.
            if (article.raw_payload or {}).get("is_press_release"):
                components["quality"] *= 0.4
            prescored.append((article, source, components))

        prescored.sort(key=lambda t: _partial_score(t[2]), reverse=True)
        shortlist = prescored[:LLM_CANDIDATES]

        # ---- editorial judgement ---------------------------------------
        editorial = await self._editorial_scores(ctx, shortlist)

        ranked: list[RankedStory] = []
        for i, (article, source, components) in enumerate(shortlist):
            judgement = editorial.get(i, {})
            components |= {
                "importance": float(judgement.get("importance", 0.5)),
                "novelty": float(judgement.get("novelty", 0.5)),
                "depth": float(judgement.get("depth", 0.5)),
            }
            fit = float(judgement.get("audience_fit", 0.5))
            score = sum(WEIGHTS[k] * v for k, v in components.items()) * (0.7 + 0.3 * fit)

            members = clusters.get(str(article.id), [])
            ranked.append(
                RankedStory(
                    article_id=str(article.id),
                    title=article.title,
                    url=article.url,
                    source=source.name,
                    score=round(score, 4),
                    angle=judgement.get("angle", ""),
                    components={k: round(v, 4) for k, v in components.items()},
                    cluster_size=len(members) or 1,
                    supporting_ids=[m for m in members if m != str(article.id)][:5],
                )
            )

        ranked.sort(key=lambda s: s.score, reverse=True)
        top = ranked[: settings.RANK_TOP_N]

        log.info(
            "ranking_completed",
            candidates=len(prescored),
            scored_by_llm=len(shortlist),
            returned=len(top),
            top_score=top[0].score if top else 0,
        )
        ctx.state["ranking"] = [asdict(s) for s in top]
        return top

    async def _editorial_scores(
        self, ctx: AgentContext, shortlist: list[tuple[RawArticle, Source, dict]]
    ) -> dict[int, dict]:
        if not shortlist:
            return {}

        payload = [
            {
                "index": i,
                "title": a.title,
                "summary": (a.raw_payload or {}).get("one_line")
                or (a.description or "")[:280],
                "source": s.name,
                "categories": a.categories,
                "hours_old": _hours_old(a.published_at or a.collected_at),
                "engagement": a.popularity_raw or {},
                "cluster_size": 1,
            }
            for i, (a, s, _) in enumerate(shortlist)
        ]

        provider = get_provider()
        try:
            resp = await provider.complete(
                system=RANKER_SYSTEM,
                prompt=json.dumps(payload, ensure_ascii=False, default=str),
                tier="fast",
                max_tokens=8000,
                json_schema=RANKER_SCHEMA,
            )
        except Exception as exc:
            # Degrade to pure deterministic ranking rather than failing the run.
            log.warning("editorial_ranking_failed", error=str(exc))
            return {}

        ctx.add_usage(resp.usage)
        await record_cost(ctx.db, resp.provider, resp.model, "llm", resp.usage)

        return {
            entry["index"]: entry
            for entry in (resp.parsed or {}).get("rankings", [])
            if isinstance(entry.get("index"), int)
        }


def _hours_old(when: datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 3600)


def _recency(when: datetime) -> float:
    """Exponential decay with a 12-hour half-life.

    Tech news decays fast: a 12-hour-old story is worth about half a
    fresh one, and a two-day-old story is essentially stale.
    """
    return round(math.exp(-_hours_old(when) / 17.31), 4)  # ln(2)*12/0.4805


def _partial_score(components: dict) -> float:
    return sum(WEIGHTS[k] * v for k, v in components.items() if k in WEIGHTS)
