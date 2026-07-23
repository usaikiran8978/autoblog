"""Seed catalogue of sources. Loaded into the `sources` table on first boot
(`make seed`); after that the table is authoritative and editable via the API.

`trust` drives the ranker's source-authority term. First-party vendor
engineering blogs score highest — when OpenAI and a news aggregator both
describe an OpenAI launch, the primary source should win.
"""

from __future__ import annotations

from typing import Any

AI = "artificial-intelligence"
LLM = "llms"
ENG = "engineering"
WEB = "web-development"
CLOUD = "cloud"
STARTUP = "startups"

SEED_SOURCES: list[dict[str, Any]] = [
    # ------------------------------------------------- first-party AI labs
    {"slug": "openai-blog", "name": "OpenAI Blog", "kind": "rss",
     "url": "https://openai.com/blog/rss.xml",
     "categories": [AI, LLM, "openai"], "trust": 0.98},
    {"slug": "anthropic-news", "name": "Anthropic News", "kind": "rss",
     "url": "https://www.anthropic.com/news/rss.xml",
     "categories": [AI, LLM, "anthropic"], "trust": 0.98},
    {"slug": "google-ai-blog", "name": "Google AI Blog", "kind": "rss",
     "url": "https://blog.google/technology/ai/rss/",
     "categories": [AI, "google-ai"], "trust": 0.96},
    {"slug": "deepmind", "name": "Google DeepMind", "kind": "rss",
     "url": "https://deepmind.google/blog/rss.xml",
     "categories": [AI, "google-ai"], "trust": 0.96},
    {"slug": "microsoft-ai", "name": "Microsoft AI Blog", "kind": "rss",
     "url": "https://blogs.microsoft.com/ai/feed/",
     "categories": [AI, "microsoft-ai"], "trust": 0.94},
    {"slug": "meta-ai", "name": "Meta AI", "kind": "rss",
     "url": "https://ai.meta.com/blog/rss/",
     "categories": [AI, "meta-ai"], "trust": 0.94},
    {"slug": "nvidia-blog", "name": "NVIDIA Blog", "kind": "rss",
     "url": "https://blogs.nvidia.com/feed/",
     "categories": [AI, "nvidia", "robotics"], "trust": 0.93},
    {"slug": "apple-ml", "name": "Apple Machine Learning Research", "kind": "rss",
     "url": "https://machinelearning.apple.com/rss.xml",
     "categories": [AI, "apple-ai"], "trust": 0.93},
    {"slug": "aws-ml", "name": "AWS Machine Learning Blog", "kind": "rss",
     "url": "https://aws.amazon.com/blogs/machine-learning/feed/",
     "categories": [AI, "amazon-ai", CLOUD], "trust": 0.90},
    {"slug": "aws-news", "name": "AWS News Blog", "kind": "rss",
     "url": "https://aws.amazon.com/blogs/aws/feed/",
     "categories": [CLOUD, "amazon-ai", "devops"], "trust": 0.90},
    {"slug": "huggingface", "name": "Hugging Face Blog", "kind": "rss",
     "url": "https://huggingface.co/blog/feed.xml",
     "categories": [AI, LLM, "generative-ai"], "trust": 0.88},

    # ------------------------------------------------------ dev / platform
    {"slug": "github-blog", "name": "GitHub Blog", "kind": "rss",
     "url": "https://github.blog/feed/",
     "categories": [ENG, "devops"], "trust": 0.90},
    {"slug": "vercel-blog", "name": "Vercel Blog", "kind": "rss",
     "url": "https://vercel.com/atom",
     "categories": [WEB, "nextjs", "react"], "trust": 0.86},
    {"slug": "react-blog", "name": "React Blog", "kind": "rss",
     "url": "https://react.dev/rss.xml",
     "categories": [WEB, "react"], "trust": 0.92},
    {"slug": "typescript-blog", "name": "TypeScript Blog", "kind": "rss",
     "url": "https://devblogs.microsoft.com/typescript/feed/",
     "categories": [WEB, "typescript"], "trust": 0.92},
    {"slug": "nodejs-blog", "name": "Node.js Blog", "kind": "rss",
     "url": "https://nodejs.org/en/feed/blog.xml",
     "categories": [WEB, "nodejs"], "trust": 0.90},
    {"slug": "python-insider", "name": "Python Insider", "kind": "rss",
     "url": "https://pythoninsider.blogspot.com/feeds/posts/default?alt=rss",
     "categories": [ENG, "python"], "trust": 0.90},
    {"slug": "langchain-blog", "name": "LangChain Blog", "kind": "rss",
     "url": "https://blog.langchain.dev/rss/",
     "categories": [AI, "langchain", "langgraph", "agentic-ai"], "trust": 0.85},
    {"slug": "cloudflare-blog", "name": "Cloudflare Blog", "kind": "rss",
     "url": "https://blog.cloudflare.com/rss/",
     "categories": [CLOUD, "devops"], "trust": 0.88},

    # ---------------------------------------------------------------- press
    {"slug": "techcrunch", "name": "TechCrunch", "kind": "rss",
     "url": "https://techcrunch.com/feed/",
     "categories": [STARTUP, AI, "product-launches"], "trust": 0.78},
    {"slug": "theverge", "name": "The Verge", "kind": "rss",
     "url": "https://www.theverge.com/rss/index.xml",
     "categories": [AI, "product-launches"], "trust": 0.76},
    {"slug": "arstechnica", "name": "Ars Technica", "kind": "rss",
     "url": "https://feeds.arstechnica.com/arstechnica/index",
     "categories": [AI, ENG], "trust": 0.80},
    {"slug": "venturebeat-ai", "name": "VentureBeat AI", "kind": "rss",
     "url": "https://venturebeat.com/category/ai/feed/",
     "categories": [AI, STARTUP], "trust": 0.74},
    {"slug": "hackernoon", "name": "HackerNoon", "kind": "rss",
     "url": "https://hackernoon.com/feed",
     "categories": [ENG, AI], "trust": 0.62},
    {"slug": "mit-tech-review-ai", "name": "MIT Technology Review", "kind": "rss",
     "url": "https://www.technologyreview.com/feed/",
     "categories": [AI, "machine-learning"], "trust": 0.85},

    # ------------------------------------------------- community / API-based
    {"slug": "hackernews", "name": "Hacker News (front page)", "kind": "api",
     "url": "https://hacker-news.firebaseio.com/v0",
     "categories": [ENG, AI, STARTUP], "trust": 0.72,
     "config": {"fetcher": "hackernews", "min_points": 100}},
    {"slug": "reddit-ml", "name": "r/MachineLearning", "kind": "api",
     "url": "https://www.reddit.com/r/MachineLearning/top.json?t=day&limit=50",
     "categories": [AI, "machine-learning"], "trust": 0.65,
     "config": {"fetcher": "reddit", "min_score": 150}},
    {"slug": "reddit-localllama", "name": "r/LocalLLaMA", "kind": "api",
     "url": "https://www.reddit.com/r/LocalLLaMA/top.json?t=day&limit=50",
     "categories": [LLM, AI], "trust": 0.62,
     "config": {"fetcher": "reddit", "min_score": 200}},
    {"slug": "github-trending", "name": "GitHub Trending", "kind": "api",
     "url": "https://api.github.com/search/repositories",
     "categories": ["github-trending", ENG], "trust": 0.70,
     "config": {"fetcher": "github_trending", "min_stars": 150}},
    {"slug": "producthunt", "name": "Product Hunt", "kind": "api",
     "url": "https://api.producthunt.com/v2/api/graphql",
     "categories": ["product-launches", STARTUP], "trust": 0.66,
     "config": {"fetcher": "producthunt"}},
    {"slug": "arxiv-ai", "name": "arXiv cs.AI", "kind": "rss",
     "url": "http://export.arxiv.org/rss/cs.AI",
     "categories": [AI, "machine-learning"], "trust": 0.82},
]

# Canonical category vocabulary the classifier is constrained to.
CATEGORIES = [
    "artificial-intelligence", "llms", "openai", "anthropic", "google-ai",
    "microsoft-ai", "meta-ai", "nvidia", "apple-ai", "amazon-ai", "robotics",
    "machine-learning", "generative-ai", "react", "nextjs", "react-native",
    "typescript", "javascript", "python", "fastapi", "nodejs", "langchain",
    "langgraph", "mcp", "agentic-ai", "cloud", "devops", "databases",
    "startups", "github-trending", "product-launches", "engineering",
    "web-development",
]
