"""A Transport Pro deployment carries no sample data — and never invents a rep.

The sample board (L1001, MC 123456, three made-up reps) exists for the offline
demo and this test suite. It used to leak into live deployments two ways: the
worker seeded the invented reps into every database, and `lanevoice-initdb`
seeded the whole board unconditionally — so a live carrier was told "let me get
you over to Sarah Chen". Now the live database is an audit trail plus a rep
directory read from `reps.toml`, sample rows already there are removed, and the
offline playground keeps its board.
"""

from __future__ import annotations

import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.datasource import open_database
from lanevoice.db import Database, Repository
from lanevoice.db.seed import purge_seed
from lanevoice.reps import load_reps, sync_reps
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer


def _live(tmp_path, **overrides):
    return get_settings().model_copy(update={
        "data_source": "transportpro", "db_path": str(tmp_path / "audit.db"), **overrides})


def _counts(db: Database) -> dict[str, int]:
    conn = db.connect()
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                for table in ("loads", "carriers", "carrier_emails", "reps")}
    finally:
        conn.close()


def test_a_live_database_gets_no_sample_rows(tmp_path):
    db = open_database(_live(tmp_path))
    assert _counts(db) == {"loads": 0, "carriers": 0, "carrier_emails": 0, "reps": 0}


def test_sample_rows_left_by_an_offline_start_are_purged_and_real_rows_kept(tmp_path):
    settings = _live(tmp_path)
    db = Database(settings.db_path)
    db.init(seed=True)                          # what an offline start used to leave behind
    conn = db.connect()
    conn.execute("INSERT INTO reps VALUES ('real', 'Real Person', '+12605551234', 1)")
    conn.commit()
    conn.close()

    removed = purge_seed(db)

    assert removed == {"loads": 5, "carrier_emails": 12, "carriers": 6, "reps": 3}
    repo = Repository(db)
    assert repo.get_rep("real").name == "Real Person"
    assert repo.get_rep("R01") is None          # Sarah Chen is gone
    assert purge_seed(db) == {}                 # and running it again removes nothing


def test_opening_the_live_database_purges_what_an_old_initdb_wrote(tmp_path):
    settings = _live(tmp_path)
    Database(settings.db_path).init(seed=True)
    db = open_database(settings)
    assert _counts(db) == {"loads": 0, "carriers": 0, "carrier_emails": 0, "reps": 0}


def test_the_rep_directory_file_is_the_source_of_truth(tmp_path):
    (tmp_path / "reps.toml").write_text(
        '[[reps]]\nid = "jsmith"\nname = "Jordan Smith"\nphone = "+12605551234"\n\n'
        '[[reps]]\nid = "alee"\nname = "Alex Lee"\nphone = "+12605555678"\navailable = false\n',
        encoding="utf-8")
    db = open_database(_live(tmp_path))         # REPS_FILE default: next to the database
    repo = Repository(db)
    assert repo.get_rep("jsmith").available is True
    assert repo.get_rep("alee").available is False
    assert repo.available_rep().rep_id == "jsmith"

    # Edited file, next start: the table follows it, nothing is merely appended.
    (tmp_path / "reps.toml").write_text(
        '[[reps]]\nid = "alee"\nname = "Alex Lee"\nphone = "+12605555678"\n', encoding="utf-8")
    repo = Repository(open_database(_live(tmp_path)))
    assert repo.get_rep("jsmith") is None
    assert repo.get_rep("alee").available is True


def test_a_malformed_rep_entry_is_refused_with_the_field_named(tmp_path):
    path = tmp_path / "reps.toml"
    path.write_text('[[reps]]\nid = "x"\nname = "X"\nphone = "call me"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="phone number"):
        load_reps(path)
    path.write_text('[[reps]]\nid = "x"\nname = "X"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="'phone'"):
        load_reps(path)
    path.write_text('[[reps]]\nid = "x"\nname = "X"\nphone = "+12605551234"\n'
                    '[[reps]]\nid = "x"\nname = "Y"\nphone = "+12605551235"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_reps(path)


def test_no_file_leaves_the_table_alone_and_an_empty_file_clears_it(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init(seed=True)
    assert load_reps(tmp_path / "missing.toml") is None
    sync_reps(db, None)
    assert Repository(db).get_rep("R01") is not None
    (tmp_path / "empty.toml").write_text("", encoding="utf-8")
    sync_reps(db, load_reps(tmp_path / "empty.toml"))
    assert Repository(db).available_rep() is None


def test_offline_mode_still_carries_the_sample_board(tmp_path):
    settings = get_settings().model_copy(update={
        "data_source": "sqlite", "db_path": str(tmp_path / "demo.db")})
    repo = Repository(open_database(settings))
    assert repo.get_load("L1001") is not None
    assert repo.get_rep("R01") is not None


def test_with_nobody_to_hand_to_the_agent_logs_a_callback_and_names_no_one(repo):
    conn = repo._db.connect()
    conn.execute("DELETE FROM reps")
    conn.commit()
    conn.close()
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    agent.handle("about L1001")
    agent.handle("MC 777111")                   # recently reactivated -> a rep
    agent.handle("yes, that's us")
    assert agent.summary()["outcome"] == "transferred"
    directive = agent._composer.turns[-1]["directive"].lower()
    assert "call them back" in directive
    assert "putting them through" not in directive
    assert "sarah" not in directive and "chen" not in directive
    assert "Rep taking the call" not in " ".join(t["facts"] for t in agent._composer.turns)
