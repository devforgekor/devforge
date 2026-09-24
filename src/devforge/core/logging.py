"""Structured logging via structlog — centralized configuration.

Provides:
  - get_logger(): returns a pre-configured structlog logger
  - setup_logging(): call once at app startup

Uses the canonical structlog + stdlib integration: events are wrapped with
``ProcessorFormatter.wrap_for_formatter`` and rendered exactly ONCE by the
stdlib ``ProcessorFormatter`` (avoids double emission / ``_from_structlog`` noise).

Usage:
    from devforge.core.logging import get_logger
    log = get_logger()
    log.info("event_name", key="value")
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import structlog
from structlog.processors import JSONRenderer as _JSONRenderer
from structlog.stdlib import ProcessorFormatter

from devforge.core.telemetry import inject_trace_context

_JSON_RENDERER = _JSONRenderer


def setup_logging(
    level: str = "INFO",
    format_json: bool = False,
    component: str = "devforge",
) -> structlog.BoundLogger:
    """Configure structlog + stdlib logging and return a logger.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        format_json: If True, output JSON; else pretty console format
        component: Component name for log context
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        inject_trace_context,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # structlog → stdlib: wrap so the ProcessorFormatter renders the event once.
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer: Any = _JSON_RENDERER() if format_json else structlog.dev.ConsoleRenderer()
    formatter = ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    logging.basicConfig(level=numeric_level, handlers=[handler], force=True)

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
