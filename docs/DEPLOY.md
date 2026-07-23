# Deployment

**Frontend → Vercel. Backend → Render (or Railway / Fly / a VPS).**

You run these commands — they authenticate as you, against your accounts.

---

## Why the backend is not on Vercel

Not a configuration issue. The architecture does not fit the platform:

| Requirement | Vercel |
|---|---|
| Celery **worker** — always-on process consuming a queue | No always-on process model |
| Celery **Beat** — persistent scheduler firing at 9:00 / 18:00 | No persistent scheduler |
| Pipeline runs of **5–20 minutes** (writer agent alone is 180–400 s) | Functions are request-scoped, capped in the low minutes even on paid plans |
| **PostgreSQL + pgvector** and **Redis** | Not hosted by Vercel |
| Filesystem writes for generated hero images | Ephemeral filesystem |

Vercel Cron does not solve this — it fires an HTTP request, which is still
bound by the function timeout.

**The frontend, however, is an ideal Vercel workload**: a static SPA with
immutable hashed assets.

---

## Step 1 — Backend (Render)

Do this first. The frontend needs the API URL.

```bash
# 1. Push the repo to GitHub (Render deploys from git)
git init && git add -A
git commit -m "AutoBlog: AI tech blog automation platform"
git remote add origin git@github.com:<you>/autoblog.git
git push -u origin main
```

2. Go to **render.com → New → Blueprint** and select the repo.
   Render reads [`render.yaml`](../render.yaml) and provisions:

   | Service | Type | Notes |
   |---|---|---|
   | `autoblog-api` | Web | Runs `alembic upgrade head` then uvicorn |
   | `autoblog-worker` | Background worker | Celery, both queues |
   | `autoblog-beat` | Background worker | **Keep at exactly 1 instance** |
   | `autoblog-db` | PostgreSQL 16 | Migration 0001 creates pgvector |
   | `autoblog-redis` | Key Value | Broker + cache + rate limits |

3. Set the secrets marked `sync: false` in the dashboard:

   ```
   ANTHROPIC_API_KEY      sk-ant-...
   OPENAI_API_KEY         sk-...          (needed for embeddings even on Claude)
   WORDPRESS_URL          (optional until you publish to a CMS)
   WORDPRESS_USERNAME
   WORDPRESS_PASSWORD
   ALERT_WEBHOOK_URL      (optional)
   ```

   `SECRET_KEY` and `ADMIN_API_KEY` are auto-generated — copy `ADMIN_API_KEY`
   out of the dashboard, you need it to trigger runs.

4. Seed the source registry from the Render shell:

   ```bash
   python -c "from app.workers.tasks import seed_sources; print(seed_sources())"
   ```

5. Verify:

   ```bash
   curl https://autoblog-api.onrender.com/api/v1/health/deep
   ```

> ⚠️ **Beat must never autoscale.** Two Beat processes publish two editions per
> slot. The blueprint pins one instance; don't change it.

> ⚠️ **Render's starter disk is ephemeral.** Generated images vanish on
> redeploy. For production set `IMAGE_STORAGE=s3` with `S3_BUCKET` and
> `S3_PUBLIC_BASE_URL`, or attach a Render persistent disk.

### Alternatives

| Platform | Fit | Notes |
|---|---|---|
| **Railway** | Excellent | Add three services from the same Dockerfile with different start commands, plus Postgres + Redis plugins. |
| **Fly.io** | Excellent | `fly.toml` with `[processes]` for `api`, `worker`, `beat`. Fly Postgres supports pgvector. |
| **VPS + docker compose** | Cheapest (~$40/mo) | Already documented — see [BLUEPRINT §10](BLUEPRINT.md#10-deployment-guide). One `docker compose --profile production up -d`. |
| **AWS ECS / GCP Cloud Run + jobs** | Most control | Cloud Run needs a separate always-on service for Beat. |

---

## Step 2 — Frontend (Vercel)

```bash
npm i -g vercel
cd frontend

vercel login          # ← opens your browser, click the link emailed to you
vercel link           # create/link the project
```

Point it at the backend, then deploy:

```bash
vercel env add VITE_API_URL production
# paste: https://autoblog-api.onrender.com

vercel --prod
```

[`vercel.json`](../frontend/vercel.json) already handles:
- **SPA rewrites** — without these `/post/:id` and `/dashboard` 404 on hard
  refresh and on any shared link
- **Immutable caching** on `/assets/*` (hashed filenames, safe to cache forever)
- Security headers (HSTS, `nosniff`, `SAMEORIGIN`, Referrer-Policy)

### Git-based deploys (recommended over CLI)

Import the repo at **vercel.com/new**, then set:

| Setting | Value |
|---|---|
| Root Directory | `frontend` |
| Framework Preset | Vite |
| Build Command | `npm run build` |
| Output Directory | `dist` |
| Environment Variable | `VITE_API_URL` = your Render API URL |

Every push to `main` then redeploys automatically, with preview deploys per PR.

---

## Step 3 — Close the CORS loop

The backend must allow the Vercel origin. In the Render dashboard set:

```
CORS_ORIGINS = https://<your-project>.vercel.app
```

Include every origin you use, comma-separated — the production domain, any
custom domain, and (if you want them to reach the API) Vercel preview URLs:

```
CORS_ORIGINS = https://autoblog.vercel.app,https://blog.yourdomain.com
```

Redeploy the API service after changing it.

> Preview deploys get a unique URL per commit, so they can't be enumerated in
> `CORS_ORIGINS`. Either point previews at a staging backend, or accept that
> previews render the empty state.

---

## Verification checklist

```bash
# backend is alive and publishing
curl https://autoblog-api.onrender.com/api/v1/health/deep

# frontend loads and deep links resolve
curl -o /dev/null -w '%{http_code}\n' https://autoblog.vercel.app/dashboard   # 200, not 404

# CORS is actually open to the frontend
curl -sI -H 'Origin: https://autoblog.vercel.app' \
  https://autoblog-api.onrender.com/api/v1/posts | grep -i access-control
```

- [ ] `/health/deep` returns `status: ok`
- [ ] `/dashboard` returns 200 on hard refresh (SPA rewrite working)
- [ ] `access-control-allow-origin` header present
- [ ] Exactly one Beat instance running
- [ ] `ADMIN_API_KEY` copied somewhere safe
- [ ] `PUBLISH_STATUS=draft` and `HUMAN_REVIEW=true` for the first week
- [ ] `DAILY_COST_LIMIT_USD` set conservatively (blueprint defaults to `10`)

---

## Cost

| Component | Where | Monthly |
|---|---|---|
| Frontend | Vercel Hobby | **$0** |
| API | Render Starter | ~$7 |
| Worker | Render Standard | ~$25 |
| Beat | Render Starter | ~$7 |
| PostgreSQL | Render Basic 256 MB | ~$6 |
| Redis / Key Value | Render Starter | ~$10 |
| LLM usage | 2 posts/day | ~$26 |
| | **Total** | **~$81/mo** |

A single VPS running `docker compose` is roughly half that (~$40 + $26 LLM)
but you own patching and backups.
