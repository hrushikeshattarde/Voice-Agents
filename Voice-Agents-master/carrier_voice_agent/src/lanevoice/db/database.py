"""
SQLite database bootstrap: schema creation and (optional) seeding.

SQLite keeps the demo zero-setup. In production you would point `Repository`
at Postgres instead — the repository interface stays the same.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS loads (
    load_id         TEXT PRIMARY KEY,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    pickup_date     TEXT NOT NULL,
    equipment       TEXT,
    weight_lbs      INTEGER,
    open_rate       REAL NOT NULL,
    ceiling_rate    REAL NOT NULL,
    fraud_low_rate  REAL NOT NULL,
    assigned_rep_id TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    is_posted       INTEGER NOT NULL DEFAULT 1,   -- only proceed if posted
    notes           TEXT,                         -- special requirements to read to the carrier
    -- What a carrier asks about once they've heard the lane. All spoken aloud.
    miles           INTEGER,
    commodity       TEXT,
    pieces          INTEGER,
    dimensions      TEXT,
    pickup_window   TEXT,                         -- "6 AM to 3 PM"
    delivery_date   TEXT,
    delivery_window TEXT,
    load_type       TEXT NOT NULL DEFAULT 'full truckload'
);

CREATE TABLE IF NOT EXISTS carriers (
    usdot_number               TEXT PRIMARY KEY,
    mc_number                  TEXT,
    legal_name                 TEXT NOT NULL,
    authority_status           TEXT NOT NULL,
    insurance_on_file          INTEGER NOT NULL,
    authority_reactivated_days INTEGER,
    last_verified_at           TEXT,
    approved                   INTEGER NOT NULL DEFAULT 1   -- allowed to work with Circle Logistics
);

-- Every address we know for a carrier. A carrier can have several (dispatch,
-- billing, a second office), so this is one-to-many: whatever a caller gives us
-- on a booking gets checked against it and added if it's new.
CREATE TABLE IF NOT EXISTS carrier_emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    usdot_number TEXT NOT NULL,
    email        TEXT NOT NULL,          -- stored lowercase
    added_at     TEXT,
    UNIQUE (usdot_number, email)
);

CREATE TABLE IF NOT EXISTS reps (
    rep_id    TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    phone     TEXT NOT NULL,
    available INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS calls (
    call_id     TEXT PRIMARY KEY,
    load_id     TEXT,
    carrier_dot TEXT,
    start_time  TEXT,
    end_time    TEXT,
    outcome     TEXT,
    transcript  TEXT,
    caller_number TEXT
);

CREATE TABLE IF NOT EXISTS negotiation_offers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT,
    round_number INTEGER,
    offered_by   TEXT,
    amount       REAL,
    timestamp    TEXT
);

CREATE TABLE IF NOT EXISTS transfer_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         TEXT,
    rep_id          TEXT,
    transfer_result TEXT,
    timestamp       TEXT
);

CREATE TABLE IF NOT EXISTS call_notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id   TEXT,
    note      TEXT,
    timestamp TEXT
);

-- Practice mode: a rep pitching a simulated customer from the dashboard.
-- One row per session, written turn by turn like `calls` — a browser tab
-- closed mid-session loses nothing but the turn in flight. Deliberately NOT
-- a row in `calls`: these are training runs about the rep, not audit trail
-- about a carrier, and nothing downstream should ever mistake one for a call.
CREATE TABLE IF NOT EXISTS practice_sessions (
    session_id   TEXT PRIMARY KEY,
    rep_name     TEXT NOT NULL,
    manager_name  TEXT,                       -- account manager the report mails to
    manager_email TEXT,                       -- (null: rep chose not to send)
    profile_id   TEXT NOT NULL,               -- e.g. 'burned_shipper'
    profile_name TEXT NOT NULL,               -- denormalized: survives profile edits
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    turns        INTEGER NOT NULL DEFAULT 0,  -- rep turns, the billing unit
    transcript   TEXT,                        -- JSON [["rep"|"customer", line], ...]
    status       TEXT NOT NULL DEFAULT 'active',   -- active | done
    end_reason   TEXT,                        -- ended | hangup | turn_limit | abandoned
    mode         TEXT NOT NULL DEFAULT 'text',     -- text | voice
    -- Talk-time totals for the scoring metrics: rep seconds measured by the
    -- browser's push-to-talk timer, customer seconds exact from synthesis.
    -- Zero on text sessions, where word counts stand in.
    rep_audio_secs      REAL NOT NULL DEFAULT 0,
    customer_audio_secs REAL NOT NULL DEFAULT 0
);

-- One scorecard per finished practice session, written right after the judge
-- runs. session_id is the PRIMARY KEY on purpose: re-scoring a session (a
-- failed judge re-run, a rubric fix) REPLACES the report, it never stacks a
-- second verdict for a manager to pick between.
CREATE TABLE IF NOT EXISTS practice_reports (
    session_id        TEXT PRIMARY KEY,
    overall           REAL,             -- mean of the scored dimensions, code-computed
    win_condition_met INTEGER,
    scores_json       TEXT,             -- {dimension: {score, quote, comment}}
    strengths_json    TEXT,
    improvements_json TEXT,             -- [{what, why, quote, better_line}]
    metrics_json      TEXT,             -- deterministic: talk ratio, WPM, fillers…
    summary           TEXT,
    judge_error       TEXT,             -- set when the judge failed; row still lands
    judge_model       TEXT,
    created_at        TEXT,
    delivery_json     TEXT,             -- vocal-delivery verdict (voice sessions)
    -- The manager email, as it actually went: exactly one of emailed_to or
    -- email_error is set when a manager was chosen; both null when not.
    emailed_to        TEXT,
    emailed_at        TEXT,
    email_error       TEXT
);
"""


class Database:
    """Owns the SQLite file: connections, schema, seeding."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self, seed: bool = False) -> None:
        """Create or migrate the schema. `seed=True` adds the sample board — the
        offline playground and the tests; never a live deployment, whose sample
        rows `datasource.open_database` removes."""
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()
        if seed:
            from lanevoice.db.seed import seed_if_empty
            seed_if_empty(self)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring an existing database up to the current shape.

        CREATE TABLE IF NOT EXISTS never alters a table that already exists, so
        anything added after a database was created has to be patched in here.
        """
        # Columns added to `loads` after the first release. ALTER TABLE ADD COLUMN
        # is the only safe move here: it keeps existing rows and their rates.
        load_columns = {r["name"] for r in
                        conn.execute("PRAGMA table_info(loads)").fetchall()}
        for name, ddl in (
            ("miles", "INTEGER"),
            ("commodity", "TEXT"),
            ("pieces", "INTEGER"),
            ("dimensions", "TEXT"),
            ("pickup_window", "TEXT"),
            ("delivery_date", "TEXT"),
            ("delivery_window", "TEXT"),
            ("load_type", "TEXT NOT NULL DEFAULT 'full truckload'"),
        ):
            if name not in load_columns:
                conn.execute(f"ALTER TABLE loads ADD COLUMN {name} {ddl}")

        # `practice_sessions` shipped text-only; the voice columns arrived with
        # the push-to-talk loop and have to be patched onto databases created in
        # between (defaults match a text session, which is what those rows were).
        practice_columns = {r["name"] for r in
                            conn.execute("PRAGMA table_info(practice_sessions)").fetchall()}
        for name, ddl in (
            ("mode", "TEXT NOT NULL DEFAULT 'text'"),
            ("rep_audio_secs", "REAL NOT NULL DEFAULT 0"),
            ("customer_audio_secs", "REAL NOT NULL DEFAULT 0"),
            ("manager_name", "TEXT"),
            ("manager_email", "TEXT"),
        ):
            if name not in practice_columns:
                conn.execute(f"ALTER TABLE practice_sessions ADD COLUMN {name} {ddl}")

        # `practice_reports` shipped before the vocal-delivery judge and the
        # manager email existed.
        report_columns = {r["name"] for r in
                          conn.execute("PRAGMA table_info(practice_reports)").fetchall()}
        for name in ("delivery_json", "emailed_to", "emailed_at", "email_error"):
            if report_columns and name not in report_columns:
                conn.execute(f"ALTER TABLE practice_reports ADD COLUMN {name} TEXT")

        # `calls.caller_number` arrived with the per-call summary note: the number
        # the phone leg reported, so a rep reading the load knows who to ring back.
        call_columns = {r["name"] for r in
                        conn.execute("PRAGMA table_info(calls)").fetchall()}
        if call_columns and "caller_number" not in call_columns:
            conn.execute("ALTER TABLE calls ADD COLUMN caller_number TEXT")
        # `calls.carrier_name`/`carrier_mc`: a snapshot of who the carrier said
        # they were AT THE TIME of this call. The dashboard used to read the
        # name via a join to the local `carriers` table, which only exists in
        # the offline demo — a live Transport Pro deployment keeps carriers in
        # Transport Pro, not here, so that join was always empty and every real
        # call showed a bare DOT number. Recorded straight from `self.carrier`
        # when the call ends, so it survives whatever the carrier's record
        # looks like today.
        if call_columns and "carrier_name" not in call_columns:
            conn.execute("ALTER TABLE calls ADD COLUMN carrier_name TEXT")
        if call_columns and "carrier_mc" not in call_columns:
            conn.execute("ALTER TABLE calls ADD COLUMN carrier_mc TEXT")

        columns = {r["name"] for r in
                   conn.execute("PRAGMA table_info(carriers)").fetchall()}
        # `carriers.contact_email` held a single address before carrier_emails
        # existed. Fold those into the new table so nothing is lost, then drop
        # it — a stale duplicate of the truth is worse than no column.
        if "contact_email" in columns:
            conn.execute(
                """INSERT OR IGNORE INTO carrier_emails (usdot_number, email, added_at)
                   SELECT usdot_number, LOWER(contact_email), datetime('now')
                     FROM carriers WHERE contact_email IS NOT NULL""")
            try:
                conn.execute("ALTER TABLE carriers DROP COLUMN contact_email")
            except sqlite3.OperationalError:
                pass    # SQLite < 3.35: leave it, the reads ignore it anyway

    def seed_reps(self) -> None:
        """Populate only the transfer list — see `seed.seed_reps`."""
        from lanevoice.db.seed import seed_reps
        seed_reps(self)

    def reset(self, seed: bool = False) -> None:
        """Drop the file and recreate — handy for tests and repeatable demos."""
        self.path.unlink(missing_ok=True)
        self.init(seed=seed)


def main() -> None:
    """`lanevoice-initdb` entry point: the schema, the rep directory, and — only
    when asked — the sample board for the offline playground."""
    import argparse

    from lanevoice.datasource import open_database
    from lanevoice.env import load_env
    from lanevoice.settings import get_settings

    parser = argparse.ArgumentParser(description="Initialize the LaneVoice database")
    parser.add_argument(
        "--seed", action="store_true",
        help="also write the SAMPLE board (L1001, MC 123456, invented reps) — for "
             "the offline playground only; refused when DATA_SOURCE=transportpro")
    args = parser.parse_args()

    load_env()
    settings = get_settings()
    if args.seed and settings.uses_transport_pro:
        raise SystemExit(
            "--seed writes invented loads, carriers and reps, and DATA_SOURCE is "
            "transportpro: a live deployment must not carry them. Set "
            "DATA_SOURCE=sqlite for an offline playground database.")
    db = open_database(settings)
    if args.seed:
        db.init(seed=True)
    print(f"Initialized {db.path}"
          + (" with the sample board" if args.seed else ""))


if __name__ == "__main__":
    main()
