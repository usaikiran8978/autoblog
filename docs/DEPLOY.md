# Deployment

**Everything on Render**, provisioned from one blueprint file.
[`render.yaml`](../render.yaml) declares all seven pieces:

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
