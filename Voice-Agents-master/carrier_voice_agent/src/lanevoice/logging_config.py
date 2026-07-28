"""Centralized logging configuration."""

from __future__ import annotations

import logging

_CONFIGURED = False

# Third-party loggers that are far too chatty at DEBUG.
_NOISY = (
    "comtypes",
    "httpx",
    "httpcore",
    "urllib3",
)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
