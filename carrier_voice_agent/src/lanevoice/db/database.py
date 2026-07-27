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
    notes           TEXT                          -- special requirements to read to the carrier
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
    transcript  TEXT
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

    def init(self, seed: bool = True) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        if seed:
            from lanevoice.db.seed import seed_if_empty
            seed_if_empty(self)

    def reset(self, seed: bool = True) -> None:
        """Drop the file and recreate — handy for tests and repeatable demos."""
        self.path.unlink(missing_ok=True)
        self.init(seed=seed)


def main() -> None:
    """`lanevoice-initdb` entry point."""
    from lanevoice.settings import get_settings

    settings = get_settings()
    db = Database(settings.db_path)
    db.init(seed=True)
    print(f"Initialized {db.path}")


if __name__ == "__main__":
    main()
