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
