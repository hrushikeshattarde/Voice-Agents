"""
Seed data — stands in for a near-real-time mirror of Transport Pro (PRD §6).
Agent's max offer on a load = ceiling_rate - settings.negotiation_buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lanevoice.db.database import Database

# load_id, origin, dest, pickup, equipment, weight,
#   open(=Load Board Rate/floor), ceiling(=Max Buy/cap), fraud_low,
#   rep, status, is_posted, notes
_LOADS = [
    ("L1001", "Chicago, IL", "Dallas, TX", "2026-07-25", "Dry Van", 42000,
     2000, 2500, 1400, "R01", "open", 1, None),
    ("L1002", "Atlanta, GA", "Miami, FL", "2026-07-24", "Reefer", 38000,
     1400, 1850, 1000, "R02", "open", 1,
     "This reefer has to run at zero degrees the whole way, and it's a strict "
     "8 A.M. pickup appointment — the driver has to be on time."),
    ("L1003", "Los Angeles, CA", "Phoenix, AZ", "2026-07-26", "Flatbed", 45000,
     900, 1250, 650, "R01", "open", 1, None),
    ("L1004", "Newark, NJ", "Boston, MA", "2026-07-23", "Dry Van", 30000,
     700, 950, 500, "R03", "covered", 1, None),
    # Not posted -> the agent won't proceed with it.
    ("L1005", "Denver, CO", "Kansas City, MO", "2026-07-28", "Dry Van", 36000,
     1100, 1400, 800, "R02", "open", 0, None),
]

# usdot, mc, name, authority, insured, reactivated_days, last_verified, approved
_CARRIERS = [
    ("DOT1000001", "MC123456", "Blue Sky Logistics LLC", "active", 1, None, None, 1),
    ("DOT2000002", "MC654321", "Roadrunner Freight Inc", "active", 1, None, None, 1),
    ("DOT3000003", "MC999888", "Ghost Carrier LLC", "revoked", 0, None, None, 1),
    ("DOT4000004", "MC777111", "Reactivated Haulers", "active", 1, 12, None, 1),
    # Verified/insured, but NOT approved to work with Circle Logistics.
    ("DOT5000005", "MC222333", "Banned Freight Co", "active", 1, None, None, 0),
]

# Addresses already known for each carrier — every carrier has several, the way
# a real one does (dispatch, billing, an after-hours desk). Whatever a caller
# gives on a booking is checked against these and appended if it's new.
_CARRIER_EMAILS = [
    ("DOT1000001", "dispatch@blueskylogistics.com"),
    ("DOT1000001", "billing@blueskylogistics.com"),
    ("DOT1000001", "afterhours@blueskylogistics.com"),
    ("DOT2000002", "ops@roadrunnerfreight.com"),
    ("DOT2000002", "dispatch@roadrunnerfreight.com"),
    ("DOT3000003", "dispatch@ghostcarrier.com"),
    ("DOT3000003", "accounts@ghostcarrier.com"),
    ("DOT4000004", "dispatch@reactivatedhaulers.com"),
    ("DOT4000004", "safety@reactivatedhaulers.com"),
    ("DOT5000005", "dispatch@bannedfreightco.com"),
    ("DOT5000005", "billing@bannedfreightco.com"),
]

# rep_id, name, phone, available
_REPS = [
    ("R01", "Sarah Chen", "+15551110101", 1),
    ("R02", "Mike Torres", "+15551110102", 1),
    ("R03", "Priya Nair", "+15551110103", 0),
]


def seed_if_empty(db: Database) -> None:
    """Fill in anything missing. Every insert is OR IGNORE, so a database that's
    already half-populated (an older one being migrated, say) tops up instead of
    tripping over a unique constraint — and existing rows are never clobbered."""
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] > 0:
            return
        conn.executemany(
            "INSERT OR IGNORE INTO loads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", _LOADS)
        conn.executemany(
            "INSERT OR IGNORE INTO carriers (usdot_number, mc_number, legal_name, "
            "authority_status, insurance_on_file, authority_reactivated_days, "
            "last_verified_at, approved) VALUES (?,?,?,?,?,?,?,?)",
            _CARRIERS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO carrier_emails (usdot_number, email, added_at)"
            " VALUES (?,?,datetime('now'))",
            _CARRIER_EMAILS,
        )
        conn.executemany("INSERT OR IGNORE INTO reps VALUES (?,?,?,?)", _REPS)
        conn.commit()
    finally:
        conn.close()
