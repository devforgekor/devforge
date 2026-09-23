"""
Custom exception hierarchy for DevForge.
"""


class DevForgeError(Exception):
    """Base exception for all DevForge errors."""

    pass


class ConfigurationError(DevForgeError):
    """Configuration-related errors."""

    pass


class DatabaseError(DevForgeError):
    """Database-related errors."""

    pass


class LLMError(DevForgeError):
    """LLM provider errors."""

    pass


class PipelineError(DevForgeError):
    """Pipeline execution errors."""

    pass


class ValidationError(DevForgeError):
    """Data validation errors."""

    pass
