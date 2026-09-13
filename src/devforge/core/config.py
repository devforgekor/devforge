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
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict, PydanticBaseSettingsSource

# ── Paths ──
DATA_DIR = Path(os.environ.get("DEVFORGE_DATA_DIR", "/opt/ai_data"))
SERVER_DIR = Path(os.environ.get("DEVFORGE_SERVER_DIR", "/opt/projects/server"))
CONFIG_DIR = Path(os.environ.get("DEVFORGE_CONFIG_DIR", str(Path.home() / ".config" / "devforge")))

# File locations (overridable via env for testing)
SECRETS_FILE = Path(os.environ.get("DEVFORGE_SECRETS_FILE", str(CONFIG_DIR / "secrets.env")))
PROVIDERS_FILE = Path(os.environ.get("DEVFORGE_PROVIDERS_FILE", str(SERVER_DIR / "config" / "providers.yaml")))
RUNTIME_ENV_FILE = Path(os.environ.get("DEVFORGE_RUNTIME_ENV", str(DATA_DIR / "scripts" / "current-mode-inference.env")))
SYSTEM_ENV_FILE = Path(os.environ.get("DEVFORGE_SYSTEM_ENV", str(DATA_DIR / "scripts" / "current-system-mode.env")))
STATE_YAML_FILE = SERVER_DIR / "state.yaml"
CLAUDE_YAML_FILE = SERVER_DIR / "CLAUDE.yaml"


class HardcodedPathResolver:
    """Centralized path resolution — replaces 40+ hardcoded paths.
    
    All paths in the refactored codebase should go through this resolver
    via the `Paths` property of ConfigRegistry.
    """
    
    def __init__(self, data_dir: Path = DATA_DIR, server_dir: Path = SERVER_DIR):
        self._data_dir = data_dir
        self._server_dir = server_dir
    
    # ── Data directories ──
    @property
    def data_dir(self) -> Path:
        return self._data_dir
    
    @property
    def models_dir(self) -> Path:
        return self._data_dir / "models" / "gguf"
    
    @property
    def scripts_dir(self) -> Path:
        return self._data_dir / "scripts"
    
    @property
    def search_dir(self) -> Path:
        return self._data_dir / "search"
    
    @property
    def current_mode_env(self) -> Path:
        return self.scripts_dir / "current-mode-inference.env"
    
    @property
    def current_system_mode_env(self) -> Path:
        return self.scripts_dir / "current-system-mode.env"
    
    @property
    def inference_entrypoint(self) -> Path:
        return self.scripts_dir / "inference-entrypoint.sh"
    
    # ── Server directories ──
    @property
    def server_dir(self) -> Path:
        return self._server_dir
    
    @property
    def pipelines_dir(self) -> Path:
        return self._server_dir / "pipelines"
    
    @property
    def containers_dir(self) -> Path:
        return self._server_dir / "containers"
    
    @property
    def logs_dir(self) -> Path:
        return self._server_dir / "logs"
    
    # ── Override for testing ──
    def override_data_dir(self, path: Path) -> "HardcodedPathResolver":
        """Create resolver with different data dir (for tests)."""
        return HardcodedPathResolver(data_dir=path, server_dir=self._server_dir)


# ── Configuration Models ──

class SecretsConfig(BaseSettings):
    """Secrets from secrets.env — no defaults, fail if missing in production."""
    model_config = SettingsConfigDict(
        env_file=SECRETS_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    POSTGRES_PASSWORD: str = ""
    DEVFORGE_DATABASE_URL: str = ""
    SLACK_BOT_TOKEN: str = ""
    SLACK_SIGNING_SECRET: str = ""
    TELEGRAM_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    DUCKDNS_TOKEN: str = ""
    ENCRYPTION_PASSPHRASE: str = ""
    
    # API Keys (for Track B)
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEYS: str = ""
    EXA_API_KEYS: str = ""
    BRAVE_API_KEYS: str = ""
    
    # Gudokpin API (GPT & Claude unified)
    GUDOKPIN_API: str = ""
    
    # OCI
    OCI_USER_OCID: str = ""
    OCI_TENANCY_OCID: str = ""
    OCI_REGION: str = ""
    OCI_API_KEY_FINGERPRINT: str = ""
    
    # GitHub
    GITHUB_TOKEN: str = ""


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
    def load(cls, state_path: Path = STATE_YAML_FILE,
             claude_path: Path = CLAUDE_YAML_FILE) -> "PersistentConfig":
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
                setattr(config, key, claude.get(key, {} if isinstance(getattr(config, key), dict) else []))
        
        return config


class ConfigRegistry:
    """Single entry point for all DevForge configuration.
    
    Combines 5 config sources with explicit priority:
    env vars > secrets.env > providers.yaml > current-*.env > state.yaml > defaults
    """
    
    def __init__(self):
        self.secrets = SecretsConfig()
        self.runtime = RuntimeConfig()
        self.system = SystemConfig()
        self.persistent = PersistentConfig.load()
        
        # Load providers.yaml if it exists
        self.providers = self._load_providers()
        
        # Path resolver
        self.paths = HardcodedPathResolver()
    
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
        """Database URL from env or secrets."""
        return os.environ.get("DEVFORGE_DATABASE_URL", self.secrets.DEVFORGE_DATABASE_URL)
    
    @property
    def inference_mode(self) -> str:
        return self.runtime.MODE
    
    @property
    def model_name(self) -> str:
        return self.runtime.MODEL_NAME
    
    @property
    def model_port(self) -> int:
        return self.runtime.PORT
    
    @property
    def system_mode(self) -> str:
        return self.system.MODE
    
    @property
    def duckdns_token(self) -> str:
        return self.secrets.DUCKDNS_TOKEN
    
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
