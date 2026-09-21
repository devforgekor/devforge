"""Tests for core.exceptions module."""

import pytest

from devforge.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    DevForgeError,
    LLMError,
    PipelineError,
    ValidationError,
)


class TestExceptionHierarchy:
    """Tests for exception hierarchy."""

    def test_devforge_error_is_base_exception(self):
        """Test DevForgeError inherits from Exception."""
        assert issubclass(DevForgeError, Exception)

    def test_configuration_error_inherits_from_devforge_error(self):
        """Test ConfigurationError inherits from DevForgeError."""
        assert issubclass(ConfigurationError, DevForgeError)

    def test_database_error_inherits_from_devforge_error(self):
        """Test DatabaseError inherits from DevForgeError."""
        assert issubclass(DatabaseError, DevForgeError)

    def test_llm_error_inherits_from_devforge_error(self):
        """Test LLMError inherits from DevForgeError."""
        assert issubclass(LLMError, DevForgeError)

    def test_pipeline_error_inherits_from_devforge_error(self):
        """Test PipelineError inherits from DevForgeError."""
        assert issubclass(PipelineError, DevForgeError)

    def test_validation_error_inherits_from_devforge_error(self):
        """Test ValidationError inherits from DevForgeError."""
        assert issubclass(ValidationError, DevForgeError)

    def test_exceptions_can_be_raised_and_caught(self):
        """Test exceptions can be raised and caught."""
        with pytest.raises(DevForgeError):
            raise DevForgeError("test")

        with pytest.raises(ConfigurationError):
            raise ConfigurationError("config error")

        with pytest.raises(DatabaseError):
            raise DatabaseError("db error")

        with pytest.raises(LLMError):
            raise LLMError("llm error")

        with pytest.raises(PipelineError):
            raise PipelineError("pipeline error")

        with pytest.raises(ValidationError):
            raise ValidationError("validation error")

    def test_exceptions_catch_base_exception(self):
        """Test catching DevForgeError catches all subclasses."""
        with pytest.raises(DevForgeError):
            raise ConfigurationError("config error")

        with pytest.raises(DevForgeError):
            raise DatabaseError("db error")

        with pytest.raises(DevForgeError):
            raise LLMError("llm error")

        with pytest.raises(DevForgeError):
            raise PipelineError("pipeline error")

        with pytest.raises(DevForgeError):
            raise ValidationError("validation error")
