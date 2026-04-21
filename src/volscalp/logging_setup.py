"""Structured logging configuration.

Uses structlog with JSON output for machine parsing and a human-friendly
console renderer when stderr is a TTY. Every log line carries an ISO
timestamp in UTC so cross-host correlation is straightforward.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO", json_output: bool = True, path: str | None = None) -> None:
    """Configure root logging + structlog.

    Safe to call multiple times; only the first call installs handlers.
    """
    if getattr(configure_logging, "_configured", False):
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    handlers: list[logging.Handler] = []

    # Stderr handler — human format if TTY, JSON if not.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level.upper())
    handlers.append(stderr_handler)

    # Rotating file handler if a path is configured.
    if path:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setLevel(level.upper())
        handlers.append(file_handler)

    for h in handlers:
        root.addHandler(h)

    use_json = json_output or not sys.stderr.isatty()
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    configure_logging._configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
