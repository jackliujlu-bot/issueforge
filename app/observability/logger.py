"""Structured logging via structlog.

We expose two functions:
    - :func:`configure_logging`: idempotent setup, called once at process start.
    - :func:`get_logger`: bound logger keyed by name.

Stdlib logging is funneled into structlog so libraries using ``logging`` get the
same JSON / console formatting.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_configured = False


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Initialize structlog. Safe to call multiple times.

    Args:
        level: standard log level name.
        fmt: ``"console"`` for human-friendly output, ``"json"`` for machine logs.
    """
    global _configured
    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if fmt == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
        force=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> Any:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
