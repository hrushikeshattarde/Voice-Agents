import pytest

from lanevoice.db import Database, Repository


@pytest.fixture
def repo(tmp_path):
    """A fresh, seeded SQLite repository in a temp dir per test."""
    db = Database(tmp_path / "test.db")
    db.reset(seed=True)
    return Repository(db)
