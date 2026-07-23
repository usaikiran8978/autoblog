"""Central configuration. Every tunable lives here and is driven by .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


def _as_list(raw: str | list[str] | None) -> list[str]:
    """Parse a delimited setting into a list.

    Accepts CSV (`a,b,c`) and JSON (`["a","b"]`), because operators reach for
    both and a deploy should not fail over the difference.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    value = raw.strip()
    if value.startswith("["):
        import json

        try:
            return [str(x).strip() for x in json.loads(value) if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass  # fall through to CSV
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------------------------------------------------------------- app
    APP_NAME: str = "autoblog"
    ENV: Literal["dev", "staging", "prod"] = "dev"
    LOG_LEVEL: str = "INFO"
    API_PREFIX: str = "/api/v1"
    ADMIN_API_KEY: str = Field(..., min_length=16)
    SECRET_KEY: str = Field(..., min_length=32)
    # Comma-separated, NOT list[str].
    #
    # pydantic-settings JSON-decodes complex types (list, dict) inside the env
    # source *before* any validator runs, so a plain CSV env var raises
    # SettingsError and no `mode="before"` validator can rescue it. Keeping
    # these as `str` and parsing in a property is version-proof.
    # Read via `settings.cors_origins`.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:4173,http://localhost:3000"

    # Regex alternative, checked in addition to the exact list above.
    #
    # PaaS platforms append a random suffix when a service name is taken
    # (autoblog-frontend -> autoblog-frontend-a1b2.onrender.com), so an exact
    # origin cannot be known at blueprint-authoring time. A narrow pattern
    # matches the real host without opening the API to every origin.
    CORS_ORIGIN_REGEX: str | None = None

    # ---------------------------------------------------------- datastores
    DATABASE_URL: PostgresDsn
    REDIS_URL: RedisDsn
    CELERY_BROKER_URL: RedisDsn | None = None
    CELERY_RESULT_BACKEND: RedisDsn | None = None

    # Vector store. `pgvector` keeps the stack to one database; `qdrant`
    # is the escape hatch once the corpus outgrows a single Postgres box.
    VECTOR_BACKEND: Literal["pgvector", "qdrant"] = "pgvector"
    QDRANT_URL: str | None = None
    QDRANT_API_KEY: str | None = None
    QDRANT_COLLECTION: str = "articles"

    # ---------------------------------------------------------------- LLM
    MODEL_PROVIDER: Literal["openai", "claude"] = "claude"
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None

    # Two tiers: `SMART` writes the article, `FAST` does classification,
    # ranking rationales and social copy. Splitting them is the single
    # biggest cost lever in the pipeline (see docs/BLUEPRINT.md §14).
    OPENAI_MODEL_SMART: str = "gpt-5.1"
    OPENAI_MODEL_FAST: str = "gpt-5.1-mini"
    ANTHROPIC_MODEL_SMART: str = "claude-opus-4-8"
    ANTHROPIC_MODEL_FAST: str = "claude-haiku-4-5"
    ANTHROPIC_EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    EMBEDDING_PROVIDER: Literal["openai", "local"] = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536
    LOCAL_EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    LLM_MAX_RETRIES: int = 4
    LLM_TIMEOUT_SECONDS: int = 600
    LLM_MAX_CONCURRENCY: int = 6

    # -------------------------------------------------------------- images
    IMAGE_PROVIDER: Literal["openai", "flux", "stability", "none"] = "openai"
    OPENAI_IMAGE_MODEL: str = "gpt-image-1"
    IMAGE_SIZE: str = "1536x1024"  # closest 16:9-ish size the API offers
    REPLICATE_API_TOKEN: str | None = None
    FLUX_MODEL: str = "black-forest-labs/flux-1.1-pro"
    STABILITY_API_KEY: str | None = None
    IMAGE_STORAGE: Literal["local", "s3"] = "local"
    IMAGE_LOCAL_DIR: str = "/data/images"
    S3_BUCKET: str | None = None
    S3_REGION: str = "us-east-1"
    S3_PUBLIC_BASE_URL: str | None = None

    # ---------------------------------------------------------- publishing
    # Comma-separated. Read via `settings.publish_targets`. See CORS_ORIGINS.
    PUBLISH_TARGETS: str = "markdown"  # wordpress|ghost|medium|custom|markdown
    PUBLISH_STATUS: Literal["draft", "publish"] = "draft"

    WORDPRESS_URL: str | None = None
    WORDPRESS_USERNAME: str | None = None
    WORDPRESS_PASSWORD: str | None = None  # application password, not login pw

    GHOST_ADMIN_API_URL: str | None = None
    GHOST_ADMIN_API_KEY: str | None = None  # "<id>:<secret>"

    MEDIUM_INTEGRATION_TOKEN: str | None = None
    MEDIUM_AUTHOR_ID: str | None = None

    CUSTOM_CMS_URL: str | None = None
    CUSTOM_CMS_TOKEN: str | None = None

    MARKDOWN_OUTPUT_DIR: str = "/data/posts"
    SITE_BASE_URL: str = "https://example.com"
    SITE_NAME: str = "Example Tech Blog"
    AUTHOR_NAME: str = "Editorial Desk"

    # --------------------------------------------------------- scheduling
    TIMEZONE: str = "Asia/Kolkata"
    SCHEDULE: str = "0 9,18 * * *"  # 9:00 AM and 6:00 PM, TIMEZONE-local
    RUN_ON_STARTUP: bool = False

    # ---------------------------------------------------------- pipeline
    COLLECT_LOOKBACK_HOURS: int = 24
    MAX_ITEMS_PER_SOURCE: int = 40
    DEDUPE_SIMILARITY_THRESHOLD: float = 0.86
    RANK_TOP_N: int = 10
    ARTICLE_MIN_WORDS: int = 1500
    ARTICLE_MAX_WORDS: int = 2500
    POSTS_PER_RUN: int = 1
    HUMAN_REVIEW: bool = False  # True => stop at `ready_for_review`

    HTTP_USER_AGENT: str = "AutoBlogBot/1.0 (+https://example.com/bot)"
    SCRAPE_ENABLED: bool = True
    RESPECT_ROBOTS_TXT: bool = True
    REQUEST_TIMEOUT_SECONDS: int = 20

    # ------------------------------------------------------------ budget
    DAILY_COST_LIMIT_USD: float = 25.0
    ALERT_WEBHOOK_URL: str | None = None
    SENTRY_DSN: str | None = None

    @property
    def cors_origins(self) -> list[str]:
        return _as_list(self.CORS_ORIGINS)

    @property
    def publish_targets(self) -> list[str]:
        return _as_list(self.PUBLISH_TARGETS)

    @property
    def broker_url(self) -> str:
        return str(self.CELERY_BROKER_URL or self.REDIS_URL)

    @property
    def result_backend(self) -> str:
        return str(self.CELERY_RESULT_BACKEND or self.REDIS_URL)

    @property
    def sqlalchemy_url(self) -> str:
        """asyncpg driver for the app, psycopg for Alembic (see migrations/env.py)."""
        return str(self.DATABASE_URL).replace("postgresql://", "postgresql+asyncpg://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
