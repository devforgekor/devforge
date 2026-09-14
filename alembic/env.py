"""Alembic environment configuration for DevForge.

Uses the async SQLAlchemy engine (asyncpg) — no psycopg2 dependency.
Migrations are generated from src/devforge/domain/models.py.

Usage (run where the DB is reachable, e.g. inside the svc pod):
    alembic upgrade head
    alembic revision --autogenerate -m "description"
    alembic downgrade -1
    alembic stamp head       # baseline an existing DB without re-running DDL
    alembic current
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# ── Import our models metadata ──
# This file is <repo>/alembic/env.py → repo root is parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from devforge.core.config import get_config  # noqa: E402
from devforge.domain.models import metadata_obj  # noqa: E402

# ── Alembic Config object ──
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Set target metadata ──
target_metadata = metadata_obj

# App-owned table names (the only ones Alembic manages).
_APP_TABLES = {t.name for t in metadata_obj.tables.values()}


def include_object(obj, name, type_, reflected, compare_to):
    """Restrict autogenerate to the app-owned tables, additively.

    - Only the 16 app tables are managed (the live DB has other subsystems' tables).
    - DB-only columns/indexes/unique-constraints are ignored so autogenerate never
      proposes destructive drops (expand/contract per ADR-0004).
    - Foreign keys are NOT filtered: reflected FKs must be visible to pair with the
      ORM definitions (filtering them causes spurious add_fk proposals).
    """
    if type_ == "table":
        return name in _APP_TABLES
    if type_ in {"column", "index", "unique_constraint"} and reflected and compare_to is None:
        return False
    return True


def get_url() -> str:
    """Get database URL from ConfigRegistry (env → secrets.env → default), as async URL."""
    url = get_config().db_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DB connection)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine (asyncpg)."""
    connectable = create_async_engine(get_url(), poolclass=pool.NullPool, future=True)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
