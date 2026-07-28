"""
Finding the `.env`, from wherever the command was run.

One repository, two plausible places to stand: the project directory
(`carrier_voice_agent/`) and the repo root above it, which is where the `.env`
usually ends up. A `.env` path relative to the working directory therefore works
from one of them and silently does nothing from the other — and "silently does
nothing" here means the agent starts with no credentials and reports the settings
as missing, which looks like an unfilled `.env` rather than a lookup that never
found the file.

So every entry point calls `load_env()`, which searches UPWARD from the working
directory. Existing environment variables always win: a value exported in the
shell (or set by a container, or by the test suite) is a deliberate override and
must not be quietly replaced by a file on disk.
"""

from __future__ import annotations

from pathlib import Path

from lanevoice.logging_config import get_logger

logger = get_logger(__name__)


def load_env(verbose: bool = False) -> Path | None:
    """Load the nearest `.env`, searching up from the working directory.

    Returns the file it used, or None if there wasn't one — which is normal in a
    container where the environment is supplied directly. Call this BEFORE the
    first `get_settings()`: that result is cached, so a late load has no effect.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:            # pragma: no cover - python-dotenv is a dep
        return None

    found = find_dotenv(usecwd=True)
    if not found:
        if verbose:
            print("[env: no .env found — using the environment as it stands]")
        return None
    # override=False: the shell wins over the file, deliberately.
    load_dotenv(found, override=False)
    path = Path(found)
    logger.debug("loaded environment from %s", path)
    if verbose:
        print(f"[env: loaded {path}]")
    return path
