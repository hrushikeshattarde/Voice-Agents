"""
Seed data — stands in for a near-real-time mirror of Transport Pro (PRD §6).
Agent's max offer on a load = ceiling_rate - settings.negotiation_buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lanevoice.db.database import Database

# load_id, origin, dest, pickup, equipment, weight, open, ceiling, fraud_low, rep, status
_LOADS = [
    ("L1001", "Chicago, IL", "Dallas, TX", "2026-07-25", "Dry Van", 42000,
     2000, 2500, 1400, "R01", "open"),
    ("L1002", "Atlanta, GA", "Miami, FL", "2026-07-24", "Reefer", 38000,
     1400, 1850, 1000, "R02", "open"),
    ("L1003", "Los Angeles, CA", "Phoenix, AZ", "2026-07-26", "Flatbed", 45000,
     900, 1250, 650, "R01", "open"),
    ("L1004", "Newark, NJ", "Boston, MA", "2026-07-23", "Dry Van", 30000,
     700, 950, 500, "R03", "covered"),
]

# usdot, mc, name, authority, insured, reactivated_days, last_verified
_CARRIERS = [
    ("DOT1000001", "MC123456", "Blue Sky Logistics LLC", "active", 1, None, None),
    ("DOT2000002", "MC654321", "Roadrunner Freight Inc", "active", 1, None, None),
    ("DOT3000003", "MC999888", "Ghost Carrier LLC", "revoked", 0, None, None),
    ("DOT4000004", "MC777111", "Reactivated Haulers", "active", 1, 12, None),
]

# rep_id, name, phone, available
_REPS = [
    ("R01", "Sarah Chen", "+15551110101", 1),
    ("R02", "Mike Torres", "+15551110102", 1),
    ("R03", "Priya Nair", "+15551110103", 0),
]


def seed_if_empty(db: Database) -> None:
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] > 0:
            return
        conn.executemany("INSERT INTO loads VALUES (?,?,?,?,?,?,?,?,?,?,?)", _LOADS)
        conn.executemany("INSERT INTO carriers VALUES (?,?,?,?,?,?,?)", _CARRIERS)
        conn.executemany("INSERT INTO reps VALUES (?,?,?,?)", _REPS)
        conn.commit()
    finally:
        conn.close()
