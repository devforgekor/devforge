"""
Database connection management using SQLAlchemy 2.0 async.
"""

from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_config


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

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get async session (use with async with).

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
            finally:
                await session.close()

    async def dispose(self):
        """Close all connections."""
        await self.engine.dispose()


# Global instance (lazy initialization)
_gateway: Optional[DatabaseGateway] = None


def get_database() -> DatabaseGateway:
    """Get or create database gateway singleton."""
    global _gateway
    if _gateway is None:
        config = get_config()
        _gateway = DatabaseGateway(
            database_url=config.db_url,
            pool_size=5,
            max_overflow=10,
        )
    return _gateway