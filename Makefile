.DEFAULT_GOAL := help
COMPOSE := docker compose
API     := $(COMPOSE) exec -T api

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ setup
.PHONY: init
init: ## Create .env from the template
	@test -f .env || (cp .env.example .env && echo "Created .env — add your API keys before `make up`")

.PHONY: build
build: ## Build images
	$(COMPOSE) build

.PHONY: up
up: ## Start the core stack (postgres, redis, api, worker, beat)
	$(COMPOSE) up -d postgres redis
	@echo "Waiting for datastores..."
	@until $(COMPOSE) exec -T postgres pg_isready -q; do sleep 1; done
	$(COMPOSE) up -d api worker beat
	@echo "API      → http://localhost:8000/docs"
	@echo "Health   → http://localhost:8000/api/v1/health/deep"

.PHONY: up-monitoring
up-monitoring: ## Start Prometheus + Grafana + Flower as well
	$(COMPOSE) --profile monitoring up -d
	@echo "Grafana  → http://localhost:3001 (admin/admin)"
	@echo "Flower   → http://localhost:5555"

.PHONY: down
down: ## Stop everything
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop and DELETE all data volumes
	$(COMPOSE) down -v

# -------------------------------------------------------------- database
.PHONY: migrate
migrate: ## Apply database migrations
	$(API) alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	$(API) alembic revision --autogenerate -m "$(m)"

.PHONY: seed
seed: ## Load the source registry into the database
	$(API) python -c "from app.workers.tasks import seed_sources; print(seed_sources())"

.PHONY: psql
psql: ## Open a psql shell
	$(COMPOSE) exec postgres psql -U autoblog -d autoblog

# -------------------------------------------------------------- pipeline
.PHONY: run
run: ## Trigger one pipeline run now (synchronous, streams logs)
	$(API) python -c "\
from app.db.session import run_async; \
from app.agents.coordinator import Coordinator; \
r = run_async(Coordinator().run(trigger='manual')); \
print(f'run={r.run_id} status={r.status} posts={len(r.posts)} cost=\$${r.cost_usd}')"

.PHONY: run-async
run-async: ## Queue a pipeline run via the API
	@curl -sS -X POST localhost:8000/api/v1/runs \
	  -H "X-API-Key: $$(grep ^ADMIN_API_KEY .env | cut -d= -f2)" \
	  -H 'Content-Type: application/json' -d '{"trigger":"manual"}' | python -m json.tool

.PHONY: dry-run
dry-run: ## Collect + dedupe + rank only — no LLM writing, no publishing
	$(API) python -m app.scripts.dry_run

.PHONY: costs
costs: ## Show the 30-day cost report
	@curl -sS localhost:8000/api/v1/analytics/costs | python -m json.tool

.PHONY: stats
stats: ## Show pipeline statistics
	@curl -sS localhost:8000/api/v1/analytics/pipeline | python -m json.tool

# ------------------------------------------------------------------- dev
.PHONY: logs
logs: ## Tail all logs
	$(COMPOSE) logs -f --tail=100

.PHONY: logs-worker
logs-worker: ## Tail worker logs only
	$(COMPOSE) logs -f --tail=100 worker

.PHONY: shell
shell: ## Python shell inside the API container
	$(API) python

.PHONY: test
test: ## Run the test suite
	$(API) pytest -v --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Lint and type-check
	$(API) ruff check backend/
	$(API) ruff format --check backend/
	$(API) mypy backend/app --ignore-missing-imports

.PHONY: fmt
fmt: ## Auto-format
	$(API) ruff format backend/
	$(API) ruff check --fix backend/
