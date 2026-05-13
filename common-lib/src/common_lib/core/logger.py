"""
core/logger | Rotating file logger factory: idempotent setup, optional console output via LOG_CONSOLE env | build_rotating_logger()
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def build_rotating_logger(
    name: str,
    log_file: str | Path,
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Return a logger with a RotatingFileHandler. Idempotent: safe to call multiple times.

    If the LOG_CONSOLE environment variable is set to "true" (case-insensitive),
    a StreamHandler is also attached so output goes to both file and console.

    Args:
        name: Logger name (typically __name__ or a domain label).
        log_file: Path to the log file; parent directories are created automatically.
        level: Logging level (default logging.INFO).
        max_bytes: Max file size before rotation (default 5 MB).
        backup_count: Number of backup files to keep (default 5).
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    if os.environ.get("LOG_CONSOLE", "false").lower() == "true":
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger
