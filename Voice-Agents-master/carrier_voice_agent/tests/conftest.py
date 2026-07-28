import os

import pytest

from lanevoice.db import Database, Repository
from lanevoice.settings import get_settings


@pytest.fixture(scope="session", autouse=True)
def offline_data_source():
    """Pin the whole suite to the offline seed data.

    `DATA_SOURCE` defaults to `transportpro` so that a deployed worker talks to
    the real board without needing a flag set. The tests are the other way round:
    they run against the seeded SQLite fixture, with no network and no
    credentials. That difference is not cosmetic — it decides whether the agent
    listens for `L1001` or for a seven-digit Transport Pro id
    (`Settings.numeric_load_ids`), so it has to be set before anything reads the
    settings, and the cache has to be dropped either side.
    """
    previous = os.environ.get("DATA_SOURCE")
    os.environ["DATA_SOURCE"] = "sqlite"
    get_settings.cache_clear()
    yield
    if previous is None:
        del os.environ["DATA_SOURCE"]
    else:
        os.environ["DATA_SOURCE"] = previous
    get_settings.cache_clear()


@pytest.fixture
def repo(tmp_path):
    """A fresh, seeded SQLite repository in a temp dir per test."""
    db = Database(tmp_path / "test.db")
    db.reset(seed=True)
    return Repository(db)
