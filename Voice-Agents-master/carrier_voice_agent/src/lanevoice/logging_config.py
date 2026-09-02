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


# livekit-agents' own sub-DEBUG level (`logger.trace`), where it reports the
# decisions that never reach DEBUG: an STT event HELD because the agent was
# speaking, the flush that re-emits or drops held events after the agent stops.
# That is the trail for a caller who was heard by the VAD, recorded, and still
# produced no transcript. LOG_LEVEL=TRACE puts the root at DEBUG and only the
# framework's loggers at this level; nothing in it is per-frame.
TRACE_LEVEL = 5


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    trace = level.upper() == "TRACE"
    logging.basicConfig(
        level=logging.DEBUG if trace else getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    if trace:
        logging.getLogger("livekit.agents").setLevel(TRACE_LEVEL)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
