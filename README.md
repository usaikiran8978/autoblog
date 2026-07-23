# AutoBlog

An AI-powered tech blog automation platform. It collects technology news from
~30 trusted sources, removes duplicates semantically, ranks what matters,
writes original 1500–2500 word analysis, generates SEO metadata and a hero
image, publishes to your CMS, and produces platform-native social copy —
**twice a day, unattended.**

Ships with a React reader UI for browsing published articles and watching the
pipeline.

📘 **[Full engineering blueprint →](docs/BLUEPRINT.md)** — architecture,
diagrams, schema, API, deployment, security, scaling, cost.
🚀 **[Deployment runbook →](docs/DEPLOY.md)** — full stack on Render, one blueprint.

---

## What it does

```
09:00 & 18:00  ─┬─ 1  Collect       ~400 articles from RSS · APIs · web
                ├─ 2  Deduplicate    ~400 → ~90 unique (embeddings + cosine)
                ├─ 3  Rank           Top 10 (recency · authority · social · AI judgement)
                ├─ 4  Write          original article + originality & QA gates
                ├─ 5  SEO            title · meta · JSON-LD · OG · Twitter · FAQ
                ├─ 6  Image          hero illustration, 16:9, no text
                ├─ 7  Publish        WordPress · Ghost · Medium · Custom · Markdown
                ├─ 8  Social         LinkedIn · X · Facebook · Threads · Instagram
                └─ 9  Analytics      tokens · cost · traffic
```

**≈ $0.43 per post.** ~$66/month at 2 posts/day including infrastructure.

---

## Quick start

```bash
make init                 # create .env
$EDITOR .env              # add ANTHROPIC_API_KEY and OPENAI_API_KEY
make build && make up
make migrate && make seed

make dry-run              # collect + dedupe + rank only (~$0.03) — start here
make run                  # full pipeline
```

Then, in a second terminal, the reader UI:

```bash
cd frontend && npm install && npm run dev
```

| | |
|---|---|
| Reader UI | http://localhost:5173 |
| API docs | http://localhost:8000/docs |
| Deep health | http://localhost:8000/api/v1/health/deep |
| Grafana | http://localhost:3001 (`make up-monitoring`) |

> The default `PUBLISH_TARGETS=markdown` needs **no CMS credentials** — you can
> run the whole pipeline end to end and inspect real output before pointing it
> at a live site.

---

## Switching AI providers

One variable. No code changes.

```bash
MODEL_PROVIDER=claude     # Opus 4.8 writes, Haiku 4.5 does everything else
MODEL_PROVIDER=openai     # GPT-5.1 writes, GPT-5.1-mini does everything else
```

Every agent talks to a provider-agnostic interface (`app/llm/base.py`), never a
vendor SDK directly.

> **Note on model IDs:** the brief specified "GPT-5.5", which OpenAI does not
> publish. `OPENAI_MODEL_SMART` defaults to `gpt-5.1` and is env-driven — point
> it at whatever your account can reach (`GET /v1/models`).

---

## Configuration

Everything is env-driven. The knobs you will actually touch:

```bash
# schedule
SCHEDULE="0 9,18 * * *"            # crontab syntax
TIMEZONE=Asia/Kolkata              # any IANA zone

# safety rails — keep these on for the first week
PUBLISH_STATUS=draft               # draft | publish
HUMAN_REVIEW=true                  # hold every post for editorial approval
DAILY_COST_LIMIT_USD=25            # hard stop; run aborts above this

# pipeline tuning
POSTS_PER_RUN=1
RANK_TOP_N=10
DEDUPE_SIMILARITY_THRESHOLD=0.86   # lower = more aggressive dedupe
ARTICLE_MIN_WORDS=1500
ARTICLE_MAX_WORDS=2500

# destinations
PUBLISH_TARGETS=markdown           # wordpress,ghost,medium,custom,markdown
IMAGE_PROVIDER=openai              # openai | flux | stability | none
```

See [`.env.example`](.env.example) for all ~70 settings.

---

## Frontend

```
frontend/
├── src/
│   ├── pages/       Home · PostDetail · Dashboard
│   ├── components/  Layout · PostCard · Markdown · States
│   ├── hooks/       useApi · usePersistedState · useDebounced
│   └── lib/         api.js · format.js
```

| Route | What it shows |
|---|---|
| `/` | Featured lead story, card grid, live search, category filter |
| `/post/:id` | Full article — reading progress, sticky table of contents, highlights, takeaways, FAQ accordion, sources, share |
| `/dashboard` | Pipeline stats, spend by model, recent runs, manual trigger |

**Design**: warm-paper light / near-black dark (respects system preference,
toggleable, no flash on load), one signal-orange accent used sparingly,
Inter for prose with JetBrains Mono for metadata. Responsive from 360px,
keyboard-accessible focus rings, skeleton loaders that mirror the real layout
so nothing shifts on load, `prefers-reduced-motion` honoured.

```bash
cd frontend
npm run dev        # localhost:5173, proxies /api → backend (no CORS setup needed)
npm run build      # → dist/
npm run preview
```

Production build: ~14 kB gzipped app + 54 kB vendor, markdown renderer
code-split so it only loads on the article route.

---

## Architecture

**Stack**: Python 3.12 · FastAPI · PostgreSQL 16 + pgvector · Redis · Celery ·
Docker · React 18 + Vite + Tailwind

**Ten agents**, each with a typed input/output and automatic cost accounting:

| # | Agent | Does |
|---|---|---|
| 1 | Collector | Fan-out fetch, normalize, dedupe-on-insert, enrich |
| 2 | Deduplicator | URL hash → title Jaccard → cosine similarity → clustering |
| 3 | Ranker | Deterministic scoring + LLM editorial judgement |
| 4 | Writer | Original article + n-gram originality + QA gate |
| 5 | SEO | Metadata, JSON-LD `@graph`, OG, Twitter Card, FAQ |
| 6 | Image | Prompt authoring + generation + storage |
| 7 | Publisher | Independent fan-out per CMS target |
| 8 | Social | Five platform-native variants |
| 9 | Analytics | Cost rollup + traffic time series |
| 10 | Coordinator | Deterministic state machine over all of the above |

### Things worth knowing

- **The coordinator is not an LLM.** The pipeline order never varies, so a
  planner would add cost, latency and nondeterminism for nothing.
- **Originality is measured, not requested.** A 7-gram overlap check runs
  against the source text. Verbatim copy → 0.00 (blocked). Light paraphrase →
  0.62 (blocked). Original analysis → 1.00. Below 0.75 the post is held for
  review instead of published.
- **Two model tiers.** Frontier model writes; the cheap model does the other
  six stages. ~70% cost reduction.
- **Failures are isolated.** One dead RSS feed does not fail a run. A
  WordPress 500 does not roll back a successful Ghost publish.
- **Runs are idempotent.** A double-fired scheduler returns the existing run
  instead of publishing a second edition.

---

## Operating it

```bash
make run              # trigger a pipeline now
make dry-run          # collect + dedupe + rank only, no writing
make costs            # 30-day spend report
make stats            # success rate, duration, dedupe compression
make logs-worker      # tail worker logs
make test             # test suite
make psql             # database shell
```

Editorial workflow with `HUMAN_REVIEW=true`:

```bash
# review the rendered output exactly as the CMS will receive it
open http://localhost:8000/api/v1/posts/$ID/preview

curl -X PATCH .../posts/$ID/status -H "X-API-Key: $KEY" \
     -d '{"status":"approved"}'
curl -X POST  .../posts/$ID/publish -H "X-API-Key: $KEY"
```

### Recommended rollout

| Week | Config | Goal |
|---|---|---|
| 1 | `markdown` + `draft` + review | Tune prompts and ranking weights |
| 2 | `wordpress` + `draft` + review | Verify CMS formatting and SEO fields |
| 3 | `publish` + review | Editor approves each post |
| 4+ | `HUMAN_REVIEW=false` | Fully autonomous |

---

## Monitoring

`/metrics` exposes pipeline duration, per-agent timings, token usage, spend,
source errors, dedupe rate and queue depth. Prometheus alert rules ship in
[`docker/alerts.yml`](docker/alerts.yml).

The alert that matters is `NoPostsPublished` — a green liveness probe on a
pipeline that has not published in 14 hours is a false negative.
`GET /api/v1/health/deep` answers the real question: *are we actually
publishing?*

---

## Cost

| Stage | Model | Cost |
|---|---|---|
| Classification · ranking · SEO · social · QA | Haiku 4.5 | $0.14 |
| **Writing** | **Opus 4.8** | **$0.25** |
| Image | gpt-image-1 | $0.04 |
| Embeddings | text-embedding-3-small | $0.001 |
| | **Per post** | **≈ $0.43** |

Implemented optimizations: two-tier models, prompt caching on the stable system
prefix, Redis embedding cache (30-day, content-hashed), batch embeddings,
selective enrichment of only the top 60 articles, lexical prefilter before
paying for embeddings, 40-candidate shortlist for LLM ranking, targeted
expansion instead of regeneration, conditional GET on feeds, and a hard budget
guard.

---

## Security

- Constant-time API key comparison on every mutating endpoint
- **SSRF guard** — outbound URLs from third-party feeds are DNS-resolved and
  rejected if they land in private/loopback/link-local space
- robots.txt honoured, 1 req/s per host, excerpt-only storage (6 kB cap)
- Non-root container, no compilers in the runtime image
- Markdown rendered with raw HTML disabled; all interpolated values escaped
- Stack traces logged, never returned to clients
- AI disclosure and source attribution on every published post

---

## Project layout

```
backend/app/
├── agents/       10 agents + coordinator
├── llm/          provider abstraction · pricing · budget guard
├── services/     fetchers · embeddings · vector · images · publishers
├── prompts/      every prompt + JSON schema
├── core/         logging · resilience · metrics · security
├── db/           SQLAlchemy models + pgvector
├── api/v1/       REST routes
└── workers/      Celery app + tasks

frontend/src/     React reader UI
docs/BLUEPRINT.md Full engineering specification
migrations/       Alembic
docker/           Dockerfile · nginx · prometheus · alerts
```

---

## Known gaps

- **Social copy is generated and stored, not auto-posted.** Wiring to the
  LinkedIn/X APIs or Buffer is an integration point, not a rewrite.
- **Analytics ships a Plausible adapter only.** GA4 is a drop-in replacement
  for one function.
- **Ranking weights are hand-tuned.** The analytics→ranker feedback loop
  described in [BLUEPRINT §15](docs/BLUEPRINT.md#15-future-improvements) is the
  highest-value next addition.
- **Medium's write API is effectively frozen** — kept for existing tokens only.
- The frontend has been built and smoke-tested (assets, routing, SPA fallback,
  API contract) but **not visually reviewed in a browser** — worth a look on
  your machine before you ship it.
