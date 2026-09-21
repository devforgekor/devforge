"""Tests for core.paths module."""

from pathlib import Path

from devforge.core.paths import Paths, get_paths


class TestPaths:
    """Tests for Paths class."""

    def test_init_with_defaults(self):
        """Test Paths initialization with default directories."""
        paths = Paths()
        assert paths.data_dir == Path("/opt/ai_data")
        assert paths.server_dir == Path("/opt/projects/server")

    def test_init_with_custom_dirs(self):
        """Test Paths initialization with custom directories."""
        custom_data = Path("/tmp/test_data")
        custom_server = Path("/tmp/test_server")

        paths = Paths(data_dir=custom_data, server_dir=custom_server)
        assert paths.data_dir == custom_data
        assert paths.server_dir == custom_server

    def test_models_dir(self):
        """Test models_dir property."""
        paths = Paths()
        assert paths.models_dir == Path("/opt/ai_data/models/gguf")

    def test_scripts_dir(self):
        """Test scripts_dir property."""
        paths = Paths()
        assert paths.scripts_dir == Path("/opt/ai_data/scripts")

    def test_search_dir(self):
        """Test search_dir property."""
        paths = Paths()
        assert paths.search_dir == Path("/opt/ai_data/search")

    def test_current_mode_env(self):
        """Test current_mode_env property."""
        paths = Paths()
        assert paths.current_mode_env == Path("/opt/ai_data/scripts/current-mode-inference.env")

    def test_current_system_mode_env(self):
        """Test current_system_mode_env property."""
        paths = Paths()
        assert paths.current_system_mode_env == Path("/opt/ai_data/scripts/current-system-mode.env")

    def test_inference_entrypoint(self):
        """Test inference_entrypoint property."""
        paths = Paths()
        assert paths.inference_entrypoint == Path("/opt/ai_data/scripts/inference-entrypoint.sh")

    def test_backups_dir(self):
        """Test backups_dir property."""
        paths = Paths()
        assert paths.backups_dir == Path("/opt/ai_data/backups")

    def test_pg_dumps_dir(self):
        """Test pg_dumps_dir property."""
        paths = Paths()
        assert paths.pg_dumps_dir == Path("/opt/ai_data/backups/pg_dumps")

    def test_src_dir(self):
        """Test src_dir property."""
        paths = Paths()
        assert paths.src_dir == Path("/opt/projects/server/src")

    def test_scripts_server_dir(self):
        """Test scripts_server_dir property."""
        paths = Paths()
        assert paths.scripts_server_dir == Path("/opt/projects/server/scripts")

    def test_docs_dir(self):
        """Test docs_dir property."""
        paths = Paths()
        assert paths.docs_dir == Path("/opt/projects/server/docs")

    def test_config_dir(self):
        """Test config_dir property."""
        paths = Paths()
        assert paths.config_dir == Path("/opt/projects/server/config")

    def test_pipelines_dir(self):
        """Test pipelines_dir property."""
        paths = Paths()
        assert paths.pipelines_dir == Path("/opt/projects/server/src/devforge/pipeline_stages")

    def test_containers_dir(self):
        """Test containers_dir property."""
        paths = Paths()
        assert paths.containers_dir == Path("/opt/projects/server/containers")

    def test_logs_dir(self):
        """Test logs_dir property."""
        paths = Paths()
        assert paths.logs_dir == Path("/opt/projects/server/logs")

    def test_temp_dir(self):
        """Test temp_dir property."""
        paths = Paths()
        assert paths.temp_dir == Path("/tmp/devforge")

    def test_checkpoint_file(self):
        """Test checkpoint_file property."""
        paths = Paths()
        assert paths.checkpoint_file == Path("/opt/projects/server/collect_checkpoint.json")

    def test_state_yaml(self):
        """Test state_yaml property."""
        paths = Paths()
        assert paths.state_yaml == Path("/opt/projects/server/state.yaml")

    def test_override_data_dir(self):
        """Test override_data_dir creates new Paths with different data dir."""
        paths = Paths()
        custom_data = Path("/tmp/custom_data")

        new_paths = paths.override_data_dir(custom_data)
        assert new_paths.data_dir == custom_data
        assert new_paths.server_dir == paths.server_dir
        assert new_paths is not paths


class TestGetPaths:
    """Tests for get_paths singleton."""

    def test_singleton_returns_same_instance(self):
        """Test get_paths returns same instance."""
        import devforge.core.paths as paths_module

        # Reset singleton for test
        paths_module._paths = None

        paths1 = get_paths()
        paths2 = get_paths()
        assert paths1 is paths2

        # Cleanup
        paths_module._paths = None

    def test_singleton_creates_on_first_call(self):
        """Test get_paths creates instance on first call."""
        import devforge.core.paths as paths_module

        # Reset singleton for test
        paths_module._paths = None

        paths = get_paths()
        assert paths is not None
        assert paths_module._paths is paths

        # Cleanup
        paths_module._paths = None
