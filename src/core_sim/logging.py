"""Structured logging to stdout.

Two deliberate choices for a benchmark rig:

* ``PrintLoggerFactory`` writes straight to stdout, bypassing the stdlib ``logging``
  machinery. Cheaper, and in K8s stdout is the log pipeline anyway.
* ``cache_logger_on_first_use=True`` — matters once you are calling this at any rate.

Per-request logging is off by default (``LOG_REQUESTS``). See config.py for why.
"""

from __future__ import annotations

import logging
import sys

import structlog
from litestar.logging import LoggingConfig

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Configure structlog for application logs. Idempotent."""
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            _LEVELS.get(level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def litestar_logging_config(level: str = "INFO") -> LoggingConfig:
    """Framework/uvicorn logs → stdout too.

    Litestar's default StreamHandler goes to *stderr*; the explicit stream override is
    the whole point of this function.
    """
    return LoggingConfig(
        root={"level": level, "handlers": ["console"]},
        formatters={
            "standard": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
        },
        handlers={
            "console": {
                "class": "logging.StreamHandler",
                "level": "DEBUG",
                "formatter": "standard",
                "stream": "ext://sys.stdout",
            },
        },
        loggers={
            "uvicorn.access": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "uvicorn.error": {"level": level, "handlers": ["console"], "propagate": False},
        },
        disable_stack_trace={404},
        log_exceptions="always",
    )


def get_logger(name: str = "core_sim") -> structlog.BoundLogger:
    return structlog.get_logger(name)
