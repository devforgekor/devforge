"""Tests for core.config DSN handling.

Why a dedicated file: DSN normalization is a config-layer concern shared by the
core engine factory and the storage adapter. No config unit test existed before
(test_database.py covers the gateway, not config), so this is the correct home
rather than overloading an unrelated test module.
"""

from types import SimpleNamespace

from devforge.core.config import ConfigRegistry, normalize_async_dsn


class TestNormalizeAsyncDsn:
    """Tests for the shared DSN normalizer."""

    def test_empty_returns_empty(self):
        assert normalize_async_dsn("") == ""

    def test_plain_postgresql_scheme_is_rewritten(self):
        assert (
            normalize_async_dsn("postgresql://user:pw@host:5432/db")
            == "postgresql+asyncpg://user:pw@host:5432/db"
        )

    def test_postgres_alias_scheme_is_rewritten(self):
        assert (
            normalize_async_dsn("postgres://user:pw@host:5432/db")
            == "postgresql+asyncpg://user:pw@host:5432/db"
        )

    def test_asyncpg_scheme_is_unchanged(self):
        url = "postgresql+asyncpg://user:pw@host:5432/db"
        assert normalize_async_dsn(url) == url

    def test_password_with_special_chars_is_preserved(self):
        assert (
            normalize_async_dsn("postgresql://postgres:devforge_secret_2026@127.0.0.1:5432/devforge_app")
            == "postgresql+asyncpg://postgres:devforge_secret_2026@127.0.0.1:5432/devforge_app"
        )

    def test_only_first_occurrence_is_rewritten(self):
        # Guards against a naive global replace mangling a path that happens to
        # contain the scheme substring.
        assert (
            normalize_async_dsn("postgresql://h/postgresql://x")
            == "postgresql+asyncpg://h/postgresql://x"
        )


class TestDbUrlAsyncProperty:
    """Tests for ConfigRegistry.db_url_async env precedence and normalization."""

    @staticmethod
    def _registry(secret_url: str = "") -> ConfigRegistry:
        # Build without touching real config files: only the secrets attribute is
        # read by db_url_async.
        registry = ConfigRegistry.__new__(ConfigRegistry)
        registry.secrets = SimpleNamespace(DEVFORGE_DATABASE_URL=secret_url)
        return registry

    def test_prefers_devforge_database_url(self, monkeypatch):
        monkeypatch.setenv("DEVFORGE_DATABASE_URL", "postgresql://a/db1")
        monkeypatch.setenv("DATABASE_URL", "postgresql://b/db2")
        assert self._registry().db_url_async == "postgresql+asyncpg://a/db1"

    def test_falls_back_to_legacy_database_url(self, monkeypatch):
        monkeypatch.delenv("DEVFORGE_DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://b/db2")
        assert self._registry().db_url_async == "postgresql+asyncpg://b/db2"

    def test_falls_back_to_secret_when_no_env(self, monkeypatch):
        monkeypatch.delenv("DEVFORGE_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert (
            self._registry("postgresql://secret/db").db_url_async
            == "postgresql+asyncpg://secret/db"
        )

    def test_returns_empty_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("DEVFORGE_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert self._registry().db_url_async == ""

    def test_db_url_raw_is_left_untouched(self, monkeypatch):
        # The raw property must NOT normalize, since it is used for display.
        monkeypatch.setenv("DEVFORGE_DATABASE_URL", "postgresql://a/db1")
        assert self._registry().db_url == "postgresql://a/db1"
