"""API-based fetchers: Hacker News, Reddit, GitHub Trending, Product Hunt.

These carry real engagement signal (points, upvotes, stars, votes), which the
ranker uses as its popularity term. RSS feeds give us breadth; these give us
the crowd's opinion of what actually matters today.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.core.logging_conf import get_logger
from app.db.models import Source
from app.services.fetchers.base import BaseFetcher, FetchedItem, clean_text

log = get_logger(__name__)


class HackerNewsFetcher(BaseFetcher):
    """Top stories above a points floor. HN's API is one request per item,
    so we bound concurrency and only pull the top slice."""

    slug = "hackernews"

    async def fetch(self, source: Source, client: httpx.AsyncClient) -> list[FetchedItem]:
        base = source.url.rstrip("/")
        min_points = int((source.config or {}).get("min_points", 100))
        limit = min(settings.MAX_ITEMS_PER_SOURCE * 3, 120)

        ids = (await client.get(f"{base}/topstories.json")).json()[:limit]
        sem = asyncio.Semaphore(10)

        async def load(item_id: int) -> dict | None:
            async with sem:
                try:
                    r = await client.get(f"{base}/item/{item_id}.json")
                    return r.json()
                except httpx.HTTPError:
                    return None

        stories = await asyncio.gather(*(load(i) for i in ids))

        items: list[FetchedItem] = []
        for story in stories:
            if not story or story.get("type") != "story" or story.get("dead"):
                continue
            points = story.get("score", 0)
            if points < min_points:
                continue
            # Self-posts have no URL; link back to the HN discussion.
            url = story.get("url") or f"https://news.ycombinator.com/item?id={story['id']}"
            items.append(
                FetchedItem(
                    title=clean_text(story.get("title")) or "",
                    url=url,
                    description=clean_text(story.get("text"), limit=800),
                    author=story.get("by"),
                    published_at=datetime.fromtimestamp(story.get("time", 0), tz=timezone.utc),
                    categories=list(source.categories or []),
                    popularity={
                        "points": points,
                        "comments": story.get("descendants", 0),
                        "platform": "hackernews",
                    },
                    raw={"hn_id": story.get("id")},
                )
            )
        return items


class RedditFetcher(BaseFetcher):
    """Public JSON endpoints. No OAuth needed for read-only `top.json`, but a
    descriptive User-Agent is mandatory or Reddit returns 429."""

    slug = "reddit"

    async def fetch(self, source: Source, client: httpx.AsyncClient) -> list[FetchedItem]:
        min_score = int((source.config or {}).get("min_score", 100))
        resp = await client.get(source.url, headers={"User-Agent": settings.HTTP_USER_AGENT})
        resp.raise_for_status()

        items: list[FetchedItem] = []
        for child in resp.json().get("data", {}).get("children", []):
            post = child.get("data", {})
            score = post.get("score", 0)
            if score < min_score or post.get("stickied"):
                continue
            permalink = f"https://www.reddit.com{post.get('permalink', '')}"
            external = post.get("url_overridden_by_dest")
            items.append(
                FetchedItem(
                    title=clean_text(post.get("title")) or "",
                    url=external or permalink,
                    description=clean_text(post.get("selftext"), limit=1200),
                    author=post.get("author"),
                    published_at=datetime.fromtimestamp(
                        post.get("created_utc", 0), tz=timezone.utc
                    ),
                    image_url=_reddit_image(post),
                    categories=list(source.categories or []),
                    popularity={
                        "score": score,
                        "comments": post.get("num_comments", 0),
                        "upvote_ratio": post.get("upvote_ratio", 0),
                        "platform": "reddit",
                    },
                    raw={"subreddit": post.get("subreddit"), "permalink": permalink},
                )
            )
        return items


def _reddit_image(post: dict) -> str | None:
    preview = post.get("preview", {}).get("images", [])
    if preview:
        return preview[0].get("source", {}).get("url", "").replace("&amp;", "&") or None
    thumb = post.get("thumbnail")
    return thumb if isinstance(thumb, str) and thumb.startswith("http") else None


class GitHubTrendingFetcher(BaseFetcher):
    """GitHub has no official trending API, so we approximate it: repos created
    in the last N days sorted by stars. Add GITHUB_TOKEN to lift the rate limit
    from 60/h to 5000/h."""

    slug = "github_trending"

    async def fetch(self, source: Source, client: httpx.AsyncClient) -> list[FetchedItem]:
        cfg = source.config or {}
        min_stars = int(cfg.get("min_stars", 150))
        days = int(cfg.get("window_days", 14))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

        headers = {"Accept": "application/vnd.github+json"}
        if token := os.getenv("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"

        resp = await client.get(
            source.url,
            params={
                "q": f"created:>{since} stars:>{min_stars}",
                "sort": "stars",
                "order": "desc",
                "per_page": 30,
            },
            headers=headers,
        )
        resp.raise_for_status()

        items: list[FetchedItem] = []
        for repo in resp.json().get("items", []):
            desc = clean_text(repo.get("description")) or ""
            items.append(
                FetchedItem(
                    title=f"{repo['full_name']} — {desc[:140]}" if desc else repo["full_name"],
                    url=repo["html_url"],
                    description=desc,
                    author=repo.get("owner", {}).get("login"),
                    published_at=_iso(repo.get("created_at")),
                    categories=list(source.categories or []),
                    tags=(repo.get("topics") or [])[:8],
                    popularity={
                        "stars": repo.get("stargazers_count", 0),
                        "forks": repo.get("forks_count", 0),
                        "language": repo.get("language"),
                        "platform": "github",
                    },
                    raw={"repo": repo["full_name"]},
                )
            )
        return items


class ProductHuntFetcher(BaseFetcher):
    """GraphQL API; requires PRODUCTHUNT_TOKEN. Silently no-ops without one so
    a missing optional credential never fails a whole run."""

    slug = "producthunt"

    QUERY = """
    query TodayPosts($first: Int!) {
      posts(order: VOTES, first: $first) {
        edges { node {
          id name tagline description url votesCount commentsCount createdAt
          thumbnail { url } topics(first: 5) { edges { node { name } } }
        } }
      }
    }
    """

    async def fetch(self, source: Source, client: httpx.AsyncClient) -> list[FetchedItem]:
        token = os.getenv("PRODUCTHUNT_TOKEN")
        if not token:
            log.info("producthunt_skipped", reason="PRODUCTHUNT_TOKEN not set")
            return []

        resp = await client.post(
            source.url,
            json={"query": self.QUERY, "variables": {"first": 20}},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()

        items: list[FetchedItem] = []
        for edge in resp.json().get("data", {}).get("posts", {}).get("edges", []):
            node = edge["node"]
            items.append(
                FetchedItem(
                    title=f"{node['name']}: {node['tagline']}",
                    url=node["url"],
                    description=clean_text(node.get("description"), limit=1000),
                    published_at=_iso(node.get("createdAt")),
                    image_url=(node.get("thumbnail") or {}).get("url"),
                    categories=list(source.categories or []),
                    tags=[t["node"]["name"] for t in node.get("topics", {}).get("edges", [])],
                    popularity={
                        "votes": node.get("votesCount", 0),
                        "comments": node.get("commentsCount", 0),
                        "platform": "producthunt",
                    },
                    raw={"ph_id": node["id"]},
                )
            )
        return items


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
