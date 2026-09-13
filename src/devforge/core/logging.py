"""Structured logging via structlog — centralized configuration.

Provides:
  - get_logger(): returns a pre-configured structlog logger
  - LoggingConfig: Pydantic-settings-based config
  - setup_logging(): call once at app startup

Usage:
    from devforge.core.logging import get_logger
    log = get_logger()
    log.info("event_name", key="value")
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

import structlog
from structlog.processors import JSONRenderer as _JSONRenderer
from structlog.stdlib import ProcessorFormatter

_JSON_RENDERER = _JSONRenderer


def setup_logging(
    level: str = "INFO",
    format_json: bool = False,
    component: str = "devforge",
) -> structlog.BoundLogger:
    """Configure structlog and return a logger instance.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        format_json: If True, output JSON; else pretty console format
        component: Component name for log context
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Configure stdlib logging
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)

    if format_json:
        formatter = ProcessorFormatter(
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
            processors=[
                _JSON_RENDERER(),
            ],
        )
    else:
        formatter = ProcessorFormatter(
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
            processors=[
                structlog.dev.ConsoleRenderer(),
            ],
        )

    handler.setFormatter(formatter)
    logging.basicConfig(
        level=numeric_level,
        handlers=[handler],
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer() if not format_json else _JSON_RENDERER(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    return get_logger(component)


def get_logger(name: Optional[str] = None) -> structlog.BoundLogger:
    """Get a configured logger.

    Args:
        name: Logger name (component context)
    """
    if name is None:
        name = "devforge"
    return structlog.get_logger(name)  # type: ignore[no-any-return]


# ── Re-export for convenience ──
__all__ = ["get_logger", "setup_logging"]
