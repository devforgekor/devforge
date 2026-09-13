"""Database gateway — async SQLAlchemy session management.

Provides a single `get_db()` dependency for FastAPI and a context
manager for standalone scripts. Connection pool is configured via
the asyncpg dialect with sensible defaults for the production
containerized environment.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.pool import NullPool

from devforge.core.config import ConfigRegistry


class DatabaseGateway:
    """Async database gateway backed by SQLAlchemy 2.0.

    Usage:
        from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
        from devforge.core.config import get_config

        config = get_config()
        gateway = DatabaseGateway.from_config(config)
        async with gateway.session() as db:
            result = await db.execute(select(Turn).where(...))
    """

    def __init__(self, db_url: str, echo: bool = False):
        # asyncpg doesn't support asyncpg+psycopg2 style; use direct asyncpg dialect
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

        # NullPool recommended for serverless / short-lived workers.
        # For long-running services, use QueuePool with pool_size=20.
        self._engine: AsyncEngine = create_async_engine(
            db_url,
            echo=echo,
            poolclass=NullPool,  # Each request gets its own connection
            pool_pre_ping=True,  # Reconnect on stale connection
            connect_args={
                "command_timeout": 30,
                "server_settings": {
                    "application_name": "devforge-server",
                },
            },
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )

    @classmethod
    def from_config(cls, config: ConfigRegistry, echo: bool = False) -> "DatabaseGateway":
        """Create gateway from ConfigRegistry."""
        return cls(config.db_url, echo=echo)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def close(self) -> None:
        """Close the engine and release all connections."""
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Context manager yielding an async session.
        
        Usage:
            async with gateway.session() as db:
                await db.execute(...)
                await db.commit()
        """
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def execute(self, stmt, *args, **kwargs):
        """Convenience: execute a statement within a session."""
        async with self.session() as db:
            result = await db.execute(stmt, *args, **kwargs)
            return result


# ── FastAPI dependency ──

_gateway: Optional[DatabaseGateway] = None


def set_gateway(gateway: DatabaseGateway) -> None:
    """Set the global gateway (call during app startup)."""
    global _gateway
    _gateway = gateway


def get_gateway() -> DatabaseGateway:
    """Get the global DatabaseGateway instance."""
    global _gateway
    if _gateway is None:
        from devforge.core.config import get_config
        config = get_config()
        _gateway = DatabaseGateway.from_config(config)
    return _gateway


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.
    
    Usage in routes:
        @router.get("/turns/{turn_id}")
        async def get_turn(turn_id: UUID, db: AsyncSession = Depends(get_db)):
            ...
    """
    gateway = get_gateway()
    async with gateway.session() as db:
        yield db
