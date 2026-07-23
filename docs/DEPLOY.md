# Deployment

Two paths. Pick one.

| | **Free** ([`render.yaml`](../render.yaml)) | **Always-on** ([`render.paid.yaml`](../render.paid.yaml)) |
|---|---|---|
| Cost | **$0** infra + ~$26/mo LLM | ~$55/mo infra + ~$26/mo LLM |
| Scheduler | GitHub Actions cron | Celery Beat |
| Pipeline runs on | GitHub Actions runner | Celery worker |
| API | Sleeps after 15 min idle (~50 s cold start) | Always warm |
| Database | Render free — **deleted after 30 days** | Managed, persistent |
| Trigger from dashboard UI | ✗ (no worker) — use the Actions tab | ✓ |

**Why there is no free always-on option:** Render's free plan has no
background workers and no cron jobs. Celery Beat and the Celery worker are
both long-lived processes, so neither can exist there. GitHub Actions replaces
both — it runs [`app/scripts/run_once.py`](../backend/app/scripts/run_once.py),
which drives the same Coordinator through the same agents in the same order.
The only thing missing is the queue, which a one-shot process doesn't need.

Jump to: [Free deployment](#free-deployment) · [Always-on deployment](#always-on-deployment)

---

# Free deployment

## 1 — Apply the blueprint

**[render.com/deploy?repo=https://github.com/usaikiran8978/autoblog](https://render.com/deploy?repo=https://github.com/usaikiran8978/autoblog)**

Creates four free services: static frontend, API, Postgres, Key Value.
No card required.

## 2 — Copy the database URL

Render dashboard → `autoblog-db` → **External Connection String**. You need the
*external* one; GitHub's runners are outside Render's private network.

## 3 — Add repository secrets

GitHub repo → **Settings → Secrets and variables → Actions → New secret**:

| Secret | Value |
|---|---|
| `DATABASE_URL` | the external string from step 2 |
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `OPENAI_API_KEY` | `sk-...` — **required for embeddings even on Claude** |

The workflow fails fast with a readable message if any are missing.

## 4 — Run it

GitHub → **Actions → Publish → Run workflow**. Tick **dry run** for the first
one: it collects, deduplicates and ranks for ~$0.03 without writing anything,
which proves the database and sources work before you spend on a writer call.

Then run it for real. Generated posts are attached to the workflow run as a
downloadable artifact, and the article appears in the reader UI.

After that it fires automatically at 03:30 and 12:30 UTC (09:00 / 18:00 IST).

## Free-tier limits worth knowing

**The database is deleted after 30 days.** Render's free Postgres is not
permanent. Before then, either upgrade it (~$6/mo) or move to
**[Neon](https://neon.tech)** — the free tier doesn't expire, supports
pgvector, and only requires swapping the `DATABASE_URL` secret.

**The API sleeps after 15 minutes idle.** The first visit takes ~50 s to wake;
the frontend shows a loading state meanwhile. Only affects the reader UI —
the pipeline runs on GitHub's infrastructure and is unaffected.

**Images are off by default.** The Actions runner's disk is destroyed when the
job ends, so a generated hero image would vanish. Posts publish without one.
To enable them, set repo variable `IMAGE_PROVIDER=openai`, `IMAGE_STORAGE=s3`,
and the `S3_BUCKET` / `AWS_*` secrets.

**Don't trigger runs from the dashboard UI.** `POST /runs` enqueues to Celery,
and there's no worker consuming that queue on the free tier — the request
succeeds and nothing happens. Use the Actions tab.

## Tuning without touching code

Repo → Settings → **Variables** (not secrets):

| Variable | Default | Effect |
|---|---|---|
| `PUBLISH_STATUS` | `draft` | `publish` to go live |
| `HUMAN_REVIEW` | `true` | `false` for fully autonomous |
| `PUBLISH_TARGETS` | `markdown` | `wordpress`, `ghost`, … |
| `IMAGE_PROVIDER` | `none` | `openai`, `flux` |
| `DAILY_COST_LIMIT_USD` | `5` | hard budget stop |

To change the schedule, edit the two `cron:` lines in
[`.github/workflows/publish.yml`](../.github/workflows/publish.yml) — they're
in **UTC**.

---

# Always-on deployment

Rename `render.paid.yaml` → `render.yaml`, push, and re-apply the blueprint.
[`render.paid.yaml`](../render.paid.yaml) declares all seven pieces:

| Service | Type | Role |
|---|---|---|
| `autoblog-frontend` | Static site | React SPA |
| `autoblog-api` | Web | FastAPI |
| `autoblog-worker` | Background worker | Celery — runs the pipeline |
| `autoblog-beat` | Background worker | Celery Beat — fires 09:00 / 18:00 |
| `autoblog-redis` | Key Value | Broker · cache · rate limits |
| `autoblog-db` | PostgreSQL 16 | + pgvector |

You run these steps — they authenticate as you, against your account.

---

## Step 1 — Push to GitHub

Render deploys from git. The repo is already committed.

```bash
# create an empty repo at github.com/new first, then:
git remote add origin git@github.com:<you>/autoblog.git
git push -u origin main
```

> `.gitignore` excludes `.env`, `node_modules`, `dist` and `/data/`. Verify
> before pushing: `git ls-files | grep -c '^\.env$'` must print `0`.

---

## Step 2 — Apply the blueprint

**render.com → New → Blueprint → select the repo → Apply.**

Render reads `render.yaml` and provisions everything. First build takes
5–10 minutes (the Docker image compiles wheels once, then caches).

### Set the secrets

Three services share an env group. Fill in the values marked `sync: false`
on **`autoblog-api`** — Render propagates them to the worker and beat:

| Key | Required | Notes |
|---|:---:|---|
| `ANTHROPIC_API_KEY` | ✅ | `sk-ant-...` |
| `OPENAI_API_KEY` | ✅ | Needed for **embeddings** even when `MODEL_PROVIDER=claude` — Anthropic has no embeddings endpoint |
| `WORDPRESS_URL` / `_USERNAME` / `_PASSWORD` | — | Only when you switch off the `markdown` target |
| `ALERT_WEBHOOK_URL` | — | Slack-compatible |

`SECRET_KEY` and `ADMIN_API_KEY` are auto-generated. **Copy `ADMIN_API_KEY`
out of the dashboard** — you need it to trigger runs from the UI or API.

### What is wired automatically

- `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL` — from the managed datastores
- `VITE_API_URL` on the frontend — from the API service's hostname, so there is
  no URL to copy by hand
- pgvector, pg_trgm and unaccent — created by migration `0001` via
  `preDeployCommand`

---

## Step 3 — Seed the sources

The pipeline has nothing to collect until the source registry is populated.
Open a shell on **`autoblog-api`** in the Render dashboard:

```bash
python -c "from app.workers.tasks import seed_sources; print(seed_sources())"
# {'created': 30, 'updated': 0}
```

---

## Step 4 — Verify

```bash
API=https://autoblog-api.onrender.com
UI=https://autoblog-frontend.onrender.com

curl -s $API/api/v1/health/deep            # status: ok, enabled_sources: 30
curl -o /dev/null -w '%{http_code}\n' $UI/dashboard   # 200 — SPA rewrite works
curl -sI -H "Origin: $UI" $API/api/v1/posts | grep -i access-control
```

Then trigger the first run from the dashboard UI (paste `ADMIN_API_KEY` into
the trigger box) or:

```bash
curl -X POST $API/api/v1/runs \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"trigger":"manual"}'
```

A full run takes 5–20 minutes. Watch `autoblog-worker` logs.

### Checklist

- [ ] `/health/deep` returns `status: ok`
- [ ] `/dashboard` returns 200 on hard refresh
- [ ] `access-control-allow-origin` header present
- [ ] `autoblog-beat` at **exactly one instance**, autoscaling off
- [ ] `ADMIN_API_KEY` saved somewhere safe
- [ ] `PUBLISH_STATUS=draft` and `HUMAN_REVIEW=true` for week one
- [ ] `DAILY_COST_LIMIT_USD` set (blueprint defaults to `10`)

---

## Things that will bite you

**Beat must never scale past one.** Two Beat processes publish two editions per
slot. The blueprint pins one instance — don't enable autoscaling on it.

**The service name determines the CORS value.** The blueprint hardcodes
`CORS_ORIGINS=https://autoblog-frontend.onrender.com`. If that name was already
taken, Render appends a suffix and CORS breaks — you'll see it immediately as a
console error on the frontend. Fix `CORS_ORIGINS` on `autoblog-api` and redeploy.

**Free and starter disks are ephemeral.** Generated hero images vanish on
redeploy. For production set `IMAGE_STORAGE=s3` plus `S3_BUCKET` and
`S3_PUBLIC_BASE_URL`, or attach a Render persistent disk to the worker.

**Starter services sleep on the free tier.** A sleeping worker misses its 09:00
slot. Use paid instances for `worker` and `beat`, or the schedule is unreliable.

**Cold starts on first deploy.** The Docker build compiles wheels for asyncpg
and friends; the first build is slow, subsequent ones hit layer cache.

---

## Cost

| Component | Plan | Monthly |
|---|---|---|
| Frontend | Static site | **$0** |
| API | Starter | ~$7 |
| Worker | Standard (writer needs headroom) | ~$25 |
| Beat | Starter | ~$7 |
| PostgreSQL | Basic 256 MB | ~$6 |
| Key Value | Starter | ~$10 |
| LLM usage | 2 posts/day | ~$26 |
| | **Total** | **~$81/mo** |

**Cheaper alternatives**

| Option | Monthly | Trade-off |
|---|---|---|
| Single VPS + `docker compose` | ~$66 | You own patching and backups. Already configured — see [BLUEPRINT §10](BLUEPRINT.md#10-deployment-guide) |
| Frontend on Vercel, rest on Render | ~$81 | No saving; adds a second platform. [`frontend/vercel.json`](../frontend/vercel.json) is still there if you want it |
| Drop images (`IMAGE_PROVIDER=none`) | −$2.50 | −$0.042/post |
| `ANTHROPIC_MODEL_SMART=claude-sonnet-5` | −$8 | ~40% cheaper writing |

---

## Other platforms

| Platform | Fit | Notes |
|---|---|---|
| **Railway** | Excellent | Three services off the same Dockerfile with different start commands, plus Postgres + Redis plugins |
| **Fly.io** | Excellent | `fly.toml` with `[processes]` for `api`, `worker`, `beat`; Fly Postgres supports pgvector |
| **VPS + compose** | Cheapest | `docker compose --profile production up -d` |
| **Vercel (backend)** | ✗ Impossible | No always-on processes, request-scoped timeouts vs. 5–20 minute runs, no Postgres/Redis, ephemeral disk |
