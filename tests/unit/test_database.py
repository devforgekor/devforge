"""Tests for core.database module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from devforge.core.database import DatabaseGateway, get_database
from devforge.core.exceptions import ConfigurationError


def _gateway_with_mock_session() -> tuple[DatabaseGateway, MagicMock, AsyncMock]:
    """Build a gateway whose engine/session are mocked (no real DB connection)."""
    engine = MagicMock()
    engine.dispose = AsyncMock()

    session = AsyncMock(spec=AsyncSession)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("devforge.core.database.create_async_engine", return_value=engine), patch(
        "devforge.core.database.async_sessionmaker",
        return_value=MagicMock(return_value=session_ctx),
    ):
        gateway = DatabaseGateway("postgresql+asyncpg://localhost/test")

    return gateway, engine, session


class TestDatabaseGateway:
    """Tests for DatabaseGateway class."""

    def test_init_with_default_params(self):
        """Test DatabaseGateway initialization with default parameters."""
        gateway = DatabaseGateway("postgresql+asyncpg://localhost/test")
        assert gateway.engine is not None
        assert gateway.session_maker is not None

    def test_init_with_custom_params(self):
        """Test DatabaseGateway initialization with custom parameters."""
        gateway = DatabaseGateway(
            "postgresql+asyncpg://user:pass@host:5432/db",
            pool_size=10,
            max_overflow=20,
        )
        assert gateway.engine is not None

    @pytest.mark.asyncio
    async def test_get_session_commits_on_success(self):
        """Test get_session commits when the block completes."""
        gateway, _engine, session = _gateway_with_mock_session()

        async with gateway.get_session() as yielded:
            assert yielded is session

        session.commit.assert_awaited_once()
        session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_session_rolls_back_on_error(self):
        """Test get_session rolls back and re-raises on error."""
        gateway, _engine, session = _gateway_with_mock_session()

        with pytest.raises(ValueError):
            async with gateway.get_session():
                raise ValueError("boom")

        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispose_calls_engine_dispose(self):
        """Test dispose awaits engine.dispose."""
        gateway, engine, _session = _gateway_with_mock_session()

        await gateway.dispose()

        engine.dispose.assert_awaited_once()


class TestGetDatabase:
    """Tests for get_database singleton."""

    def test_singleton_returns_same_instance(self):
        """Test get_database returns same instance."""
        import devforge.core.database as db_module

        db_module._gateway = None
        try:
            with patch("devforge.core.database.get_config") as mock_config:
                mock_config.return_value.db_url = "postgresql+asyncpg://localhost/test"
                mock_config.return_value.db_pool_size = 5
                mock_config.return_value.db_max_overflow = 10

                db1 = get_database()
                db2 = get_database()
                assert db1 is db2
        finally:
            db_module._gateway = None

    def test_raises_when_db_url_empty(self):
        """Test get_database raises ConfigurationError when URL is empty."""
        import devforge.core.database as db_module

        db_module._gateway = None
        try:
            with patch("devforge.core.database.get_config") as mock_config:
                mock_config.return_value.db_url = ""

                with pytest.raises(ConfigurationError):
                    get_database()
        finally:
            db_module._gateway = None
