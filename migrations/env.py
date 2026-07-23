"""Alembic environment.

Runs migrations synchronously with psycopg while the application uses asyncpg —
migrations are a one-shot startup concern and the sync driver keeps this file
simple and debuggable.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# psycopg (sync) for migrations, regardless of the app's async driver.
# Built by config so the driver is named explicitly — a bare postgresql://
# would resolve to psycopg2, which this project does not ship.
sync_url = settings.sync_database_url
config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Keep pgvector's internal objects out of autogenerate diffs."""
    if type_ == "table" and name in {"spatial_ref_sys"}:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
