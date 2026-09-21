"""
Centralized path management to eliminate hardcoded paths.
"""

from pathlib import Path
from typing import Optional

from .config import DATA_DIR, SERVER_DIR


class Paths:
    """Centralized path registry."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        server_dir: Optional[Path] = None,
    ):
        """
        Initialize paths.

        Args:
            data_dir: Override data directory (default: /opt/ai_data)
            server_dir: Override server directory (default: /opt/projects/server)
        """
        self._data_dir = data_dir or DATA_DIR
        self._server_dir = server_dir or SERVER_DIR

    # ── Data directories ──
    @property
    def data_dir(self) -> Path:
        """Main data directory."""
        return self._data_dir

    @property
    def models_dir(self) -> Path:
        """Models directory."""
        return self._data_dir / "models" / "gguf"

    @property
    def scripts_dir(self) -> Path:
        """Scripts directory."""
        return self._data_dir / "scripts"

    @property
    def search_dir(self) -> Path:
        """Search directory."""
        return self._data_dir / "search"

    @property
    def current_mode_env(self) -> Path:
        """Current mode environment file."""
        return self.scripts_dir / "current-mode-inference.env"

    @property
    def current_system_mode_env(self) -> Path:
        """Current system mode environment file."""
        return self.scripts_dir / "current-system-mode.env"

    @property
    def inference_entrypoint(self) -> Path:
        """Inference entrypoint script."""
        return self.scripts_dir / "inference-entrypoint.sh"

    @property
    def backups_dir(self) -> Path:
        """Backup directory."""
        return self._data_dir / "backups"

    @property
    def pg_dumps_dir(self) -> Path:
        """PostgreSQL dumps directory."""
        return self.backups_dir / "pg_dumps"

    # ── Server directories ──
    @property
    def server_dir(self) -> Path:
        """Server root directory."""
        return self._server_dir

    @property
    def src_dir(self) -> Path:
        """Source directory (refactored)."""
        return self._server_dir / "src"

    @property
    def scripts_server_dir(self) -> Path:
        """Scripts directory (legacy)."""
        return self._server_dir / "scripts"

    @property
    def docs_dir(self) -> Path:
        """Documentation directory."""
        return self._server_dir / "docs"

    @property
    def config_dir(self) -> Path:
        """Configuration directory."""
        return self._server_dir / "config"

    @property
    def pipelines_dir(self) -> Path:
        """Pipelines directory."""
        return self._server_dir / "src" / "devforge" / "pipeline_stages"

    @property
    def containers_dir(self) -> Path:
        """Containers directory."""
        return self._server_dir / "containers"

    @property
    def logs_dir(self) -> Path:
        """Logs directory."""
        return self._server_dir / "logs"

    @property
    def temp_dir(self) -> Path:
        """Temporary directory."""
        return Path("/tmp") / "devforge"

    # ── State files ──
    @property
    def checkpoint_file(self) -> Path:
        """Turn collection checkpoint."""
        return self._server_dir / "collect_checkpoint.json"

    @property
    def state_yaml(self) -> Path:
        """State YAML file."""
        return self._server_dir / "state.yaml"

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        for path in [
            self.logs_dir,
            self.temp_dir,
            self.backups_dir,
            self.pg_dumps_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def override_data_dir(self, path: Path) -> "Paths":
        """Create paths with different data dir (for tests)."""
        return Paths(data_dir=path, server_dir=self._server_dir)


# Global instance
_paths: Optional[Paths] = None


def get_paths() -> Paths:
    """Get paths singleton."""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths
