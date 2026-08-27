import os

import pytest

from lanevoice.db import Database, Repository
from lanevoice.settings import get_settings

# Settings whose mere PRESENCE changes which code path the agent takes, rather
# than only tuning one. A HappyRobot token switches booking from "log an offer" to
# "issue a booking link"; an office terminal code switches the board from
# company-wide to one office's subtree. A developer with a filled-in `.env` would
# otherwise run a materially different suite from CI — and the failure looks like
# a broken test, not a broken fixture.
#
# Cleared for every test. The tests that exercise those paths turn them on
# explicitly via `model_copy`, which is also what documents that they are optional.
#
# ADD TO THIS LIST whenever a new opt-in integration or scope setting lands.
_BEHAVIOUR_SWITCHING_ENV = (
    "HIGHWAY_API_TOKEN",
    "HAPPYROBOT_URL",
    "HAPPYROBOT_TOKEN",
    "TRANSPORT_PRO_OFFICE_TERMINAL_CODE",
    "TRANSPORT_PRO_OFFICE_TERMINAL_IDS",
    "TRANSPORT_PRO_EXTRA_TERMINAL_IDS",
    # SMTP_HOST + SMTP_FROM switch practice reports from "stored only" to
    # "stored and MAILED" — a filled-in .env must never make a test send email.
    "SMTP_HOST",
    "SMTP_FROM",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
)


@pytest.fixture(scope="session", autouse=True)
def offline_data_source():
    """Pin the whole suite to the offline seed data, with no live integrations.

    `DATA_SOURCE` defaults to `transportpro` so that a deployed worker talks to
    the real board without needing a flag set. The tests are the other way round:
    they run against the seeded SQLite fixture, with no network and no
    credentials. That difference is not cosmetic — it decides whether the agent
    listens for `L1001` or for a seven-digit Transport Pro id
    (`Settings.numeric_load_ids`), so it has to be set before anything reads the
    settings, and the cache has to be dropped either side.

    The same reasoning covers `_BEHAVIOUR_SWITCHING_ENV`: what is in somebody's
    local `.env` must never decide which code path the suite exercises.

    Those are set to EMPTY rather than deleted, which matters. `Settings` reads the
    `.env` FILE as well as the environment, so unsetting a variable just lets the
    file's value through — and the file is exactly what we're trying to ignore. A
    real environment variable outranks dotenv, so an empty one is what actually
    silences it.
    """
    previous = {name: os.environ.get(name)
                for name in ("DATA_SOURCE", *_BEHAVIOUR_SWITCHING_ENV)}
    os.environ["DATA_SOURCE"] = "sqlite"
    for name in _BEHAVIOUR_SWITCHING_ENV:
        os.environ[name] = ""
    get_settings.cache_clear()
    yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


@pytest.fixture
def repo(tmp_path):
    """A fresh, seeded SQLite repository in a temp dir per test."""
    db = Database(tmp_path / "test.db")
    db.reset(seed=True)
    return Repository(db)
