"""Configuration registry — single entry point for all DevForge settings.

Consolidates 5 previously-dispersed config sources into typed Pydantic models
while maintaining physical file separation:

  1. secrets.env           → SecretsConfig (env file, no defaults)
  2. providers.yaml        → ModelProvidersConfig (LLM 공급자, Track B)
  3. current-mode-inference.env → RuntimeConfig (런타임 모델 상태)
  4. current-system-mode.env    → SystemConfig (day/night 모드)
  5. state.yaml + CLAUDE.yaml → PersistentConfig (YAML 직렬화)

Priority: env vars > secrets.env > providers.yaml > current-*.env > state.yaml > defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Path roots are owned by core.paths (SSOT) and imported here.
# Why: core.paths needs these constants to build its registry, and core.config
# needs them to locate config files. When core.config owned them, core.paths had
# to import from core.config, which forced ConfigRegistry to lazy-import paths
# to dodge a circular import. Moving ownership to the lower-level module makes
# the dependency one-directional (config -> paths) and removes that workaround.
from .paths import CONFIG_DIR, DATA_DIR, SERVER_DIR, get_paths

# File locations (overridable via env for testing)
SECRETS_FILE = Path(os.environ.get("DEVFORGE_SECRETS_FILE", str(CONFIG_DIR / "secrets.env")))
PROVIDERS_FILE = Path(
    os.environ.get("DEVFORGE_PROVIDERS_FILE", str(SERVER_DIR / "config" / "providers.yaml"))
)
RUNTIME_ENV_FILE = Path(
    os.environ.get("DEVFORGE_RUNTIME_ENV", str(DATA_DIR / "scripts" / "current-mode-inference.env"))
)
SYSTEM_ENV_FILE = Path(
    os.environ.get("DEVFORGE_SYSTEM_ENV", str(DATA_DIR / "scripts" / "current-system-mode.env"))
)
STATE_YAML_FILE = SERVER_DIR / "state.yaml"
CLAUDE_YAML_FILE = SERVER_DIR / "CLAUDE.yaml"


def normalize_async_dsn(db_url: str) -> str:
    """Normalize a PostgreSQL DSN to the SQLAlchemy asyncpg dialect.

    Why: `create_async_engine()` rejects the plain libpq scheme
    (`postgresql://` / `postgres://`) used by the legacy psql-based scripts,
    requiring `postgresql+asyncpg://`. Centralizing the rewrite here keeps the
    core engine factory and the storage adapter from each maintaining their own
    copy. Empty input is returned unchanged so callers can fail fast.
    """
    if not db_url:
        return ""
    if db_url.startswith("postgresql+asyncpg://"):
        return db_url
    if db_url.startswith("postgresql://"):
        return db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgres://"):
        return db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return db_url


# ── Configuration Models ──


class SecretsConfig(BaseSettings):
    """Secrets from secrets.env — no defaults, fail if missing in production."""

    model_config = SettingsConfigDict(
        env_file=SECRETS_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DEVFORGE_POSTGRES_PASSWORD: str = ""
    DEVFORGE_DATABASE_URL: str = ""
    SLACK_BOT_TOKEN_KEY: str = ""
    SLACK_SIGNING_SECRET_KEY: str = ""
    TELEGRAM_TOKEN_KEY: str = ""
    TELEGRAM_CHAT_ID: str = ""
    DUCKDNS_TOKEN_KEY: str = ""
    DEVFORGE_ENCRYPTION_PASSPHRASE: str = ""

    # API Keys (for Track B)
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEYS: str = ""
    EXA_API_KEYS: str = ""
    BRAVE_API_KEYS: str = ""

    # Gudokpin API (GPT & Claude unified)
    GUDOKPIN_API_KEY: str = ""

    # OCI
    OCI_USER_OCID: str = ""
    OCI_TENANCY_OCID: str = ""
    OCI_REGION: str = ""
    OCI_API_KEY_FINGERPRINT: str = ""

    # GitHub
    MY_GITHUB_TOKEN_KEY: str = ""
    MY_COPILOT_GITHUB_TOKEN_KEY: str = ""


class ProviderConfig(BaseModel):
    """Single LLM provider configuration."""

    type: str  # "local", "openai", "anthropic"
    api_key: str = ""
    base_url: str = ""
    default_models: dict[str, str] = Field(default_factory=dict)


class ModelProvidersConfig(BaseModel):
    """LLM provider configuration — Track B (separate document).

    Track A에서는 local provider만 사용. Track B에서 OpenAI/Anthropic 추가.
    """

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_provider: str = "local"

    def resolve_provider_name(self, model_key: str) -> str:
        """model_key에 매핑된 provider 이름 반환.
        Track A에서는 항상 'local' 반환.
        Track B에서는 providers.yaml 매핑 조회로 확장."""
        return self.default_provider


class RuntimeConfig(BaseSettings):
    """Runtime inference configuration from current-mode-inference.env."""

    model_config = SettingsConfigDict(
        env_file=RUNTIME_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    MODE: str = "day"
    MODEL_NAME: str = "day-extractor"
    PORT: int = 8082
    MODEL_FILE: str = ""
    CTX_SIZE: int = 8192
    THREADS: int = 4
    THREADS_BATCH: int = 4
    CACHE_RAM: int = 1024
    CACHE_TYPE_K: str = "q8_0"
    CACHE_TYPE_V: str = "q8_0"


class SystemConfig(BaseSettings):
    """System mode configuration from current-system-mode.env."""

    model_config = SettingsConfigDict(
        env_file=SYSTEM_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    MODE: str = "day"


class PersistentConfig(BaseModel):
    """Persistent state from state.yaml + CLAUDE.yaml."""

    # From state.yaml
    structural: dict[str, Any] = Field(default_factory=dict)
    pipeline: dict[str, Any] = Field(default_factory=dict)
    containers: dict[str, Any] = Field(default_factory=dict)

    # From CLAUDE.yaml
    overview: dict[str, Any] = Field(default_factory=dict)
    services: list[dict[str, Any]] = Field(default_factory=list)
    network: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(
        cls, state_path: Path = STATE_YAML_FILE, claude_path: Path = CLAUDE_YAML_FILE
    ) -> PersistentConfig:
        """Load from both YAML files."""
        config = cls()

        if state_path.exists():
            with open(state_path) as f:
                state = yaml.safe_load(f) or {}
            for key in ("structural", "pipeline", "containers"):
                setattr(config, key, state.get(key, {}))

        if claude_path.exists():
            with open(claude_path) as f:
                claude = yaml.safe_load(f) or {}
            for key in ("overview", "services", "network"):
                setattr(
                    config,
                    key,
                    claude.get(key, {} if isinstance(getattr(config, key), dict) else []),
                )

        return config


class ConfigRegistry:
    """Single entry point for all DevForge configuration.

    Combines 5 config sources with explicit priority:
    env vars > secrets.env > providers.yaml > current-*.env > state.yaml > defaults
    """

    def __init__(self) -> None:
        self.secrets = SecretsConfig()
        self.runtime = RuntimeConfig()
        self.system = SystemConfig()
        self.persistent = PersistentConfig.load()

        # Load providers.yaml if it exists
        self.providers = self._load_providers()

        # Path resolver (imported at module scope — no circular dependency now
        # that core.paths owns the path constants).
        self.paths = get_paths()

    def _load_providers(self) -> ModelProvidersConfig:
        """Load providers.yaml if it exists, otherwise return default config."""
        if not PROVIDERS_FILE.exists():
            return ModelProvidersConfig(default_provider="local")

        try:
            with open(PROVIDERS_FILE) as f:
                data = yaml.safe_load(f) or {}
            return ModelProvidersConfig(**data)
        except Exception:
            return ModelProvidersConfig(default_provider="local")

    # ── Convenience properties (backwards compatibility) ──
    @property
    def db_url(self) -> str:
        """Raw database URL from env or secrets (scheme left untouched).

        Kept as-is for display/logging (e.g. status commands) where the exact
        string the operator configured should be shown. Async engine consumers
        must use `db_url_async` instead.
        """
        return os.environ.get("DEVFORGE_DATABASE_URL", self.secrets.DEVFORGE_DATABASE_URL)

    @property
    def db_url_async(self) -> str:
        """Database URL normalized for the SQLAlchemy asyncpg dialect.

        Why this exists:
        - Legacy scripts read a bare `DATABASE_URL` (psql/libpq scheme
          `postgresql://`), while the refactored config reads
          `DEVFORGE_DATABASE_URL`. Consumers that only checked one name would
          silently get an empty or wrong-scheme DSN. Reading both names here
          closes that gap.
        - `create_async_engine()` requires the `postgresql+asyncpg://` dialect;
          passing a plain `postgresql://` URL raises at engine construction.
          Normalizing the scheme in one place means the core engine factory and
          the storage adapter cannot drift apart.
        - Returns "" when nothing is configured so callers can fail fast with a
          clear ConfigurationError instead of a cryptic driver error.
        """
        raw = (
            os.environ.get("DEVFORGE_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or self.secrets.DEVFORGE_DATABASE_URL
        )
        return normalize_async_dsn(raw)

    @property
    def inference_mode(self) -> str:
        return self.runtime.MODE

    @property
    def llm_provider(self) -> str:
        return os.environ.get("DEVFORGE_LLM_PROVIDER", self.providers.default_provider)

    @property
    def model_name(self) -> str:
        return self.runtime.MODEL_NAME

    @property
    def model_port(self) -> int:
        return self.runtime.PORT

    @property
    def system_mode(self) -> str:
        return os.environ.get("DEVFORGE_SYSTEM_MODE", self.system.MODE)

    @property
    def duckdns_token(self) -> str:
        return self.secrets.DUCKDNS_TOKEN_KEY

    @property
    def db_pool_size(self) -> int:
        return int(os.environ.get("DEVFORGE_DB_POOL_SIZE", "5"))

    @property
    def db_max_overflow(self) -> int:
        return int(os.environ.get("DEVFORGE_DB_MAX_OVERFLOW", "10"))

    def reload(self) -> None:
        """Reload all config from files (for runtime config changes)."""
        self.secrets = SecretsConfig()
        self.runtime = RuntimeConfig()
        self.system = SystemConfig()
        self.persistent = PersistentConfig.load()
        self.providers = self._load_providers()


# ── Singleton ──
_config: Optional[ConfigRegistry] = None


def get_config() -> ConfigRegistry:
    """Get the global ConfigRegistry instance."""
    global _config
    if _config is None:
        _config = ConfigRegistry()
    return _config


# ── Watchdog Configuration (v2.1 — legacy parity) ──


@dataclass(frozen=True)
class WatchdogConfig:
    """Watchdog subsystem configuration.

    Defaults mirror scripts/lib/watchdog/config.py (SSOT): SERVICE_TARGETS,
    TIMER_TARGETS, LLM_TARGETS, HEARTBEAT_WORKERS. When the legacy constants
    change, update both.
    """

    failure_threshold: int = 3
    success_threshold: int = 2  # reserved; circuit is 3/120s/5 in legacy
    circuit_reset_timeout_sec: int = 120
    backoff_reset_sec: int = 600
    check_interval_sec: int = 60
    check_timeout_sec: int = 30
    state_file: str = "/opt/ai_data/scripts/watchdog_state.json"

    critical_services: list[str] = field(
        default_factory=lambda: [
            "devforge-turn-watcher",
            "openrouter-rr-proxy",
            "devforge-day-cycle",
            "ebook-watcher",
            "container-devforge-fastapi",
            "container-devforge-worker",
            "ebook-api",
            "devforge-news-api",
            "cashbook",
        ]
    )
    timers: dict[str, int] = field(
        default_factory=lambda: {  # unit -> max_idle_sec (legacy TIMER_TARGETS)
            "devforge-openrouter-free-models.timer": 93600,
            "devforge-system-sync.timer": 2700,
            "devforge-news.timer": 25200,
            "devforge-daily-structure.timer": 90000,
            "devforge-weekly-enrich-rebuild.timer": 604800,
            "devforge-restore-test.timer": 2592000,
            "devforge-backup-safety.timer": 97200,
            "devforge-dev-poll.timer": 1800,
            "devforge-news-digest.timer": 90000,
            "kuhwa-schedule.timer": 90000,
            "workspace-autopush.timer": 90000,
            "reference-monitor.timer": 604800,
            "golden-image-deploy-check.timer": 1350,
            "workspace-autocommit.timer": 2700,
            "devforge-summary-retry.timer": 10800,
            "baseline-daily.timer": 129600,
            "kv-backup.timer": 907200,
            "golden-image-yearly-check.timer": 34560000,
        }
    )
    system_service_targets: list[str] = field(
        default_factory=lambda: [
            "caddy",
            "netdata",  # rootful system services — alert-only
        ]
    )
    alert_only_targets: list[str] = field(
        default_factory=lambda: [
            "container-postgres",
            "container-devforge-mcp",
            "container-flaresolverr",
            "anthropic-openrouter-proxy",
            "anthropic-proxy",
            "or-rate-limiter",
        ]
    )
    oneshot_result_targets: list[str] = field(
        default_factory=lambda: [
            "devforge-daily-structure.service",
            "devforge-backup.service",
            "devforge-restore-test.service",
            "devforge-system-sync.service",
            "kv-backup.service",
            "workspace-autocommit.service",
            "golden-image-deploy-check.service",
            "root-volume-daily-clean.service",
            "opencode-db-offload.service",
            "reference-monitor.service",
            "devforge-dev-aging.service",
            "devforge-sp-secret-check.service",
            "devforge-mcp-inventory.service",
        ]
    )
    llm_targets: dict[str, int] = field(
        default_factory=lambda: {
            "day-extract": 8082,
            "night-verify": 8084,
        }
    )
    day_ports: list[int] = field(default_factory=lambda: [8080, 8082])
    # svc pod host port forwarding (rootlessport) — legacy SVCPOD_PUBLISHED_PORTS
    svcpod_published_ports: dict[int, str] = field(
        default_factory=lambda: {
            8000: "devforge-mcp",
            8002: "devforge-fastapi",
            8085: "blob-explorer",
            8191: "flaresolverr",
        }
    )
    llm_latency_baseline_ms: int = 2000  # legacy check_probe_latency baseline
    heartbeat_workers: dict[str, int] = field(
        default_factory=lambda: {
            "embed_batch": 1800,
            "liveness_embed_batch": 1800,
            "entity_scan": 1800,
            "text_clean": 1800,
            "day_extract": 1800,
            "day_enrich": 1800,
            "news_collector": 25200,
        }
    )
    disks: list[str] = field(default_factory=lambda: ["/", "/opt/ai_data"])
    # DataImpulse path (toki31) liveness — read-only, alert-only (no recovery).
    # Disabled by default until watchdog-standard-compliance P2.6 (single leader);
    # dry-run observation first. Signals are read-only (status.json/log/pgrep).
    dataimpulse_enabled: bool = False
    dataimpulse_status_file: str = "/opt/ai_data/flaresolverr/ebook_watcher/status.json"
    dataimpulse_log_file: str = "/opt/ai_data/flaresolverr/ebook_watcher/collect_toki31.log"
    dataimpulse_stale_sec: int = 1800
    dataimpulse_deep_stale_sec: int = 300
    dataimpulse_deep_consecutive: int = 3

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        """Load scalar settings from environment variables."""
        return cls(
            failure_threshold=int(os.getenv("WATCHDOG_FAILURE_THRESHOLD", "3")),
            success_threshold=int(os.getenv("WATCHDOG_SUCCESS_THRESHOLD", "2")),
            circuit_reset_timeout_sec=int(os.getenv("WATCHDOG_CIRCUIT_RESET_SEC", "120")),
            backoff_reset_sec=int(os.getenv("WATCHDOG_BACKOFF_RESET_SEC", "600")),
            check_interval_sec=int(os.getenv("WATCHDOG_CHECK_INTERVAL_SEC", "60")),
            check_timeout_sec=int(os.getenv("WATCHDOG_CHECK_TIMEOUT_SEC", "30")),
            state_file=os.getenv("WATCHDOG_STATE_FILE", "/opt/ai_data/scripts/watchdog_state.json"),
            dataimpulse_enabled=os.getenv("WATCHDOG_DATAIMPULSE_ENABLED", "0").lower()
            in ("1", "true", "yes", "on"),
            dataimpulse_status_file=os.getenv(
                "WATCHDOG_DATAIMPULSE_STATUS_FILE", cls.dataimpulse_status_file
            ),
            dataimpulse_log_file=os.getenv(
                "WATCHDOG_DATAIMPULSE_LOG_FILE", cls.dataimpulse_log_file
            ),
            dataimpulse_stale_sec=int(os.getenv("WATCHDOG_DATAIMPULSE_STALE_SEC", "1800")),
            dataimpulse_deep_stale_sec=int(os.getenv("WATCHDOG_DATAIMPULSE_DEEP_STALE_SEC", "300")),
            dataimpulse_deep_consecutive=int(
                os.getenv("WATCHDOG_DATAIMPULSE_DEEP_CONSECUTIVE", "3")
            ),
        )
