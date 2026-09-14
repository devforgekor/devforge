"""Alembic environment configuration for DevForge.

Uses SQLAlchemy async engine with asyncpg dialect.
Migrations are generated from src/devforge/domain/models.py.

Usage:
    alembic revision --autogenerate -m "description"
    alembic upgrade head
    alembic downgrade -1
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from alembic import context

# ── Import our models metadata ──
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from devforge.domain.models import metadata_obj  # noqa: E402
from devforge.core.config import get_config  # noqa: E402

# ── Alembic Config object ──
config = context.config

# ── Interpret the config file for logging ──
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Set target metadata ──
target_metadata = metadata_obj


def get_url() -> str:
    """Get database URL from ConfigRegistry (env → secrets.env → default)."""
    config = get_config()
    url = config.db_url
    # Alembic uses sync engine for running migrations
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using a sync engine."""
    connectable = create_engine(
        get_url(),
        poolclass=pool.NullPool,
        pool_pre_ping=True,
        future=True,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
