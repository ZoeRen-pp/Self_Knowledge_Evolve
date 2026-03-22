"""Structured logging setup."""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger. Call once at application startup."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. setup_logging() should be called first."""
    return logging.getLogger(name)