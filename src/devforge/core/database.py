"""
Database connection management using SQLAlchemy 2.0 async.

Role separation (why two gateways exist):
- This module is the low-level *core* primitive: a generic QueuePool-backed
  engine/session factory, matching REFACTORING_PLAN §3.2 (`core/database.py`).
  It has no dependency on adapters (core sits below adapters in the layering
  contract).
- `adapters/driven/storage/database_gateway.py` is the *service* gateway used by
  the FastAPI app, MCP server and extract adapter. It deliberately uses NullPool
  and adds server-side connect_args (command_timeout/application_name) suited to
  short-lived requests.
- Both therefore need the same driver URL normalization; that logic is owned by
  `ConfigRegistry.db_url_async` so the two cannot diverge.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_config
from .exceptions import ConfigurationError


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass


class DatabaseGateway:
    """Async database connection pool manager."""

    def __init__(
        self,
        database_url: str,
        pool_size: int = 5,
        max_overflow: int = 10,
    ):
        """
        Initialize async engine.

        Args:
            database_url: PostgreSQL connection string (postgresql+asyncpg://...)
            pool_size: Connection pool size
            max_overflow: Max overflow connections
        """
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            echo=False,  # Set True for SQL logging
            pool_pre_ping=True,  # Verify connections before use
        )
        self.session_maker = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """
        Yield an async session, committing on success and rolling back on error.

        Example:
            async with gateway.get_session() as session:
                result = await session.execute(select(Turn))
        """
        async with self.session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Close all connections."""
        await self.engine.dispose()


# Global instance (lazy initialization)
_gateway: Optional[DatabaseGateway] = None


def get_database() -> DatabaseGateway:
    """Get or create database gateway singleton."""
    global _gateway
    if _gateway is None:
        config = get_config()
        # Use the normalized async DSN (handles bare DATABASE_URL / libpq scheme),
        # and fail fast with a clear error rather than constructing an engine
        # from an empty string.
        if not config.db_url_async:
            raise ConfigurationError(
                "Database URL is empty. Set DEVFORGE_DATABASE_URL "
                "(postgresql+asyncpg://...) or DEVFORGE_POSTGRES_PASSWORD."
            )
        _gateway = DatabaseGateway(
            database_url=config.db_url_async,
            pool_size=config.db_pool_size,
            max_overflow=config.db_max_overflow,
        )
    return _gateway
