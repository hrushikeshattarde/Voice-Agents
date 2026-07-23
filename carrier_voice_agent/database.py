"""
database.py
-----------
SQLite data layer for the Carrier Sales Voice AI agent.

This mirrors the core entities from the PRD (§10):
    Load, Carrier, Call, NegotiationOffer, TransferEvent

SQLite is used deliberately: it needs zero setup, lives in a single file, and
runs perfectly inside Google Colab. In production you would swap this module's
implementation for Postgres (per PRD §5.6) while keeping the same function
signatures.
"""

import sqlite3
import json
import datetime
from pathlib import Path

DB_PATH = Path(__file__).with_name("carrier_agent.db")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS loads (
    load_id         TEXT PRIMARY KEY,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    pickup_date     TEXT NOT NULL,
    equipment       TEXT,
    weight_lbs      INTEGER,
    -- CARRIER-PAY economics. The carrier gets PAID; we (the broker) want to pay
    -- as little as possible. The agent OPENS low and walks its offer UP.
    open_rate       REAL NOT NULL,   -- agent's opening offer (starts here)
    ceiling_rate    REAL NOT NULL,   -- absolute max budget. Ask above this = NO DEAL, end call.
                                     -- Agent may only offer up to (ceiling - buffer); the
                                     -- buffer is reserved for a human to use on transfer.
    fraud_low_rate  REAL NOT NULL,   -- suspiciously cheap -> fraud review, don't just book
    assigned_rep_id TEXT,
    status          TEXT NOT NULL DEFAULT 'open'  -- open | covered | cancelled
);

CREATE TABLE IF NOT EXISTS carriers (
    mc_number             TEXT,
    usdot_number          TEXT PRIMARY KEY,   -- USDOT is primary per PRD §8.3
    legal_name            TEXT NOT NULL,
    authority_status      TEXT NOT NULL,      -- active | revoked | inactive
    insurance_on_file     INTEGER NOT NULL,   -- 1/0
    authority_reactivated_days INTEGER,       -- days since reactivation (fraud signal)
    last_verified_at      TEXT
);

CREATE TABLE IF NOT EXISTS reps (
    rep_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    available   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS calls (
    call_id      TEXT PRIMARY KEY,
    load_id      TEXT,
    carrier_dot  TEXT,
    start_time   TEXT,
    end_time     TEXT,
    outcome      TEXT,                -- booked | transferred | abandoned | rejected
    transcript   TEXT
);

CREATE TABLE IF NOT EXISTS negotiation_offers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT,
    round_number INTEGER,
    offered_by   TEXT,                -- carrier | agent
    amount       REAL,
    timestamp    TEXT
);

CREATE TABLE IF NOT EXISTS transfer_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         TEXT,
    rep_id          TEXT,
    transfer_result TEXT,             -- connected | voicemail | failed
    timestamp       TEXT
);

CREATE TABLE IF NOT EXISTS call_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id    TEXT,
    note       TEXT,
    timestamp  TEXT
);
"""


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH, seed: bool = True) -> None:
    """Create tables and (optionally) load sample data."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    if seed:
        _seed(conn)
    conn.close()


def reset_db(db_path: Path = DB_PATH) -> None:
    """Drop the file and reseed — handy for repeatable demos/tests."""
    Path(db_path).unlink(missing_ok=True)
    init_db(db_path, seed=True)


# --------------------------------------------------------------------------- #
# Seed data  (stands in for a Transport Pro mirror — PRD §6)
# --------------------------------------------------------------------------- #
def _seed(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] > 0:
        return  # already seeded

    loads = [
        # load_id, origin, dest, pickup, equip, weight,
        #   open(start), ceiling(hard max), fraud_low, rep, status
        #   agent's max offer = ceiling - 150 (buffer held for a human)
        ("L1001", "Chicago, IL", "Dallas, TX", "2026-07-25", "Dry Van", 42000,
         2000, 2500, 1400, "R01", "open"),   # agent can offer up to 2350
        ("L1002", "Atlanta, GA", "Miami, FL", "2026-07-24", "Reefer", 38000,
         1400, 1850, 1000, "R02", "open"),   # agent up to 1700
        ("L1003", "Los Angeles, CA", "Phoenix, AZ", "2026-07-26", "Flatbed", 45000,
         900, 1250, 650, "R01", "open"),      # agent up to 1100
        ("L1004", "Newark, NJ", "Boston, MA", "2026-07-23", "Dry Van", 30000,
         700, 950, 500, "R03", "covered"),    # agent up to 800
    ]
    conn.executemany(
        "INSERT INTO loads VALUES (?,?,?,?,?,?,?,?,?,?,?)", loads
    )

    carriers = [
        # mc, dot, name, authority, insured, reactivated_days, last_verified
        ("MC123456", "DOT1000001", "Blue Sky Logistics LLC", "active", 1, None, None),
        ("MC654321", "DOT2000002", "Roadrunner Freight Inc", "active", 1, None, None),
        ("MC999888", "DOT3000003", "Ghost Carrier LLC", "revoked", 0, None, None),
        ("MC777111", "DOT4000004", "Reactivated Haulers", "active", 1, 12, None),  # fraud signal
    ]
    conn.executemany(
        "INSERT INTO carriers VALUES (?,?,?,?,?,?,?)", carriers
    )

    reps = [
        ("R01", "Sarah Chen", "+15551110101", 1),
        ("R02", "Mike Torres", "+15551110102", 1),
        ("R03", "Priya Nair", "+15551110103", 0),  # unavailable -> tests fallback
    ]
    conn.executemany("INSERT INTO reps VALUES (?,?,?,?)", reps)
    conn.commit()


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #
def get_load(load_id: str, db_path: Path = DB_PATH):
    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM loads WHERE UPPER(load_id)=UPPER(?)", (load_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_open_loads(db_path: Path = DB_PATH):
    conn = connect(db_path)
    rows = conn.execute("SELECT * FROM loads WHERE status='open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_carrier(mc_or_dot: str, db_path: Path = DB_PATH):
    """Look up by either MC or USDOT number (digits only comparison)."""
    q = "".join(ch for ch in mc_or_dot if ch.isdigit())
    conn = connect(db_path)
    row = conn.execute(
        """SELECT * FROM carriers
           WHERE REPLACE(REPLACE(mc_number,'MC',''),' ','')=?
              OR REPLACE(REPLACE(usdot_number,'DOT',''),' ','')=?""",
        (q, q),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_rep(rep_id: str, db_path: Path = DB_PATH):
    conn = connect(db_path)
    row = conn.execute("SELECT * FROM reps WHERE rep_id=?", (rep_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_available_rep_fallback(exclude_rep_id: str = None, db_path: Path = DB_PATH):
    conn = connect(db_path)
    row = conn.execute(
        "SELECT * FROM reps WHERE available=1 AND rep_id != ? LIMIT 1",
        (exclude_rep_id or "",),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Write helpers  (audit trail — PRD §9.4)
# --------------------------------------------------------------------------- #
def start_call(call_id: str, db_path: Path = DB_PATH):
    conn = connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO calls (call_id, start_time) VALUES (?,?)",
        (call_id, _now()),
    )
    conn.commit()
    conn.close()


def log_offer(call_id, round_number, offered_by, amount, db_path: Path = DB_PATH):
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO negotiation_offers (call_id, round_number, offered_by, amount, timestamp)"
        " VALUES (?,?,?,?,?)",
        (call_id, round_number, offered_by, amount, _now()),
    )
    conn.commit()
    conn.close()


def log_note(call_id, note, db_path: Path = DB_PATH):
    """Write a free-text note against the call (e.g. 'asked above ceiling')."""
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO call_notes (call_id, note, timestamp) VALUES (?,?,?)",
        (call_id, note, _now()),
    )
    conn.commit()
    conn.close()


def log_transfer(call_id, rep_id, result, db_path: Path = DB_PATH):
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO transfer_events (call_id, rep_id, transfer_result, timestamp)"
        " VALUES (?,?,?,?)",
        (call_id, rep_id, result, _now()),
    )
    conn.commit()
    conn.close()


def book_load(load_id: str, db_path: Path = DB_PATH):
    conn = connect(db_path)
    conn.execute("UPDATE loads SET status='covered' WHERE load_id=?", (load_id,))
    conn.commit()
    conn.close()


def end_call(call_id, load_id, carrier_dot, outcome, transcript, db_path: Path = DB_PATH):
    conn = connect(db_path)
    conn.execute(
        """UPDATE calls SET load_id=?, carrier_dot=?, end_time=?, outcome=?, transcript=?
           WHERE call_id=?""",
        (load_id, carrier_dot, _now(), outcome,
         json.dumps(transcript) if not isinstance(transcript, str) else transcript,
         call_id),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
    print("Open loads:", [l["load_id"] for l in get_open_loads()])
