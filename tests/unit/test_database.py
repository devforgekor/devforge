"""Tests for core.database module."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy.ext.asyncio import AsyncSession

from devforge.core.database import DatabaseGateway, get_database


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
    async def test_get_session_context_manager(self):
        """Test get_session as async context manager."""
        gateway = DatabaseGateway("postgresql+asyncpg://localhost/test")
        
        with patch.object(gateway, 'session_maker') as mock_maker:
            mock_session = AsyncMock(spec=AsyncSession)
            mock_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_maker.return_value.__aexit__ = AsyncMock(return_value=False)
            
            async with gateway.get_session() as session:
                assert session == mock_session

    @pytest.mark.asyncio
    async def test_dispose(self):
        """Test dispose closes all connections."""
        gateway = DatabaseGateway("postgresql+asyncpg://localhost/test")
        
        with patch.object(gateway.engine, 'dispose', new_callable=AsyncMock) as mock_dispose:
            await gateway.dispose()
            mock_dispose.assert_called_once()


class TestGetDatabase:
    """Tests for get_database singleton."""

    def test_singleton_returns_same_instance(self):
        """Test get_database returns same instance."""
        import devforge.core.database as db_module
        
        # Reset singleton for test
        db_module._gateway = None
        
        with patch('devforge.core.database.get_config') as mock_config:
            mock_config.return_value.db_url = "postgresql+asyncpg://localhost/test"
            
            db1 = get_database()
            db2 = get_database()
            assert db1 is db2
        
        # Cleanup
        db_module._gateway = None

    def test_singleton_creates_on_first_call(self):
        """Test get_database creates instance on first call."""
        import devforge.core.database as db_module
        
        # Reset singleton for test
        db_module._gateway = None
        
        with patch('devforge.core.database.get_config') as mock_config:
            mock_config.return_value.db_url = "postgresql+asyncpg://localhost/test"
            
            db = get_database()
            assert db is not None
            assert db_module._gateway is db
        
        # Cleanup
        db_module._gateway = None