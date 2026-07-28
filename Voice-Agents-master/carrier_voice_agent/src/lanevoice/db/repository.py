"""
Repository — all data access lives here, returning typed domain models.

Swapping SQLite for Postgres later means reimplementing only this class; the
services and conversation layers depend on the method signatures, not on SQL.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Iterable

from lanevoice.db.database import Database
from lanevoice.domain.models import (
    AuthorityStatus,
    Carrier,
    Load,
    LoadStatus,
    OfferParty,
    Rep,
)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


class Repository:
    def __init__(self, db: Database):
        self._db = db

    # -- mappers ------------------------------------------------------------ #
    @staticmethod
    def _load(row: sqlite3.Row) -> Load:
        return Load(
            load_id=row["load_id"],
            origin=row["origin"],
            destination=row["destination"],
            pickup_date=row["pickup_date"],
            equipment=row["equipment"],
            weight_lbs=row["weight_lbs"],
            open_rate=row["open_rate"],
            ceiling_rate=row["ceiling_rate"],
            fraud_low_rate=row["fraud_low_rate"],
            assigned_rep_id=row["assigned_rep_id"],
            status=LoadStatus(row["status"]),
            is_posted=bool(row["is_posted"]),
            notes=row["notes"],
            miles=row["miles"],
            commodity=row["commodity"],
            pieces=row["pieces"],
            dimensions=row["dimensions"],
            pickup_window=row["pickup_window"],
            delivery_date=row["delivery_date"],
            delivery_window=row["delivery_window"],
            load_type=row["load_type"] or "full truckload",
        )

    @staticmethod
    def _carrier(row: sqlite3.Row, emails: tuple[str, ...] = ()) -> Carrier:
        return Carrier(
            usdot_number=row["usdot_number"],
            mc_number=row["mc_number"],
            legal_name=row["legal_name"],
            authority_status=AuthorityStatus(row["authority_status"]),
            insurance_on_file=bool(row["insurance_on_file"]),
            authority_reactivated_days=row["authority_reactivated_days"],
            last_verified_at=row["last_verified_at"],
            approved=bool(row["approved"]),
            contact_emails=emails,
        )

    @staticmethod
    def _rep(row: sqlite3.Row) -> Rep:
        return Rep(
            rep_id=row["rep_id"],
            name=row["name"],
            phone=row["phone"],
            available=bool(row["available"]),
        )

    # -- reads -------------------------------------------------------------- #
    def get_load(self, load_id: str) -> Load | None:
        conn = self._db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM loads WHERE UPPER(load_id)=UPPER(?)", (load_id,)
            ).fetchone()
            return self._load(row) if row else None
        finally:
            conn.close()

    def open_loads(self) -> list[Load]:
        """Bookable loads only: open AND posted."""
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM loads WHERE status='open' AND is_posted=1"
            ).fetchall()
            return [self._load(r) for r in rows]
        finally:
            conn.close()

    def get_carrier(self, mc_or_dot: str) -> Carrier | None:
        q = _digits(mc_or_dot)
        conn = self._db.connect()
        try:
            row = conn.execute(
                """SELECT * FROM carriers
                   WHERE REPLACE(REPLACE(mc_number,'MC',''),' ','')=?
                      OR REPLACE(REPLACE(usdot_number,'DOT',''),' ','')=?""",
                (q, q),
            ).fetchone()
            if not row:
                return None
            emails = self._emails(conn, row["usdot_number"])
            return self._carrier(row, emails)
        finally:
            conn.close()

    @staticmethod
    def _emails(conn: sqlite3.Connection, usdot_number: str) -> tuple[str, ...]:
        rows = conn.execute(
            "SELECT email FROM carrier_emails WHERE usdot_number=? ORDER BY id",
            (usdot_number,),
        ).fetchall()
        return tuple(r["email"] for r in rows)

    def carrier_emails(self, usdot_number: str) -> tuple[str, ...]:
        """Every address on file for this carrier, oldest first."""
        conn = self._db.connect()
        try:
            return self._emails(conn, usdot_number)
        finally:
            conn.close()

    def email_on_file(self, usdot_number: str, email: str) -> bool:
        return email.strip().lower() in self.carrier_emails(usdot_number)

    def get_rep(self, rep_id: str) -> Rep | None:
        conn = self._db.connect()
        try:
            row = conn.execute("SELECT * FROM reps WHERE rep_id=?", (rep_id,)).fetchone()
            return self._rep(row) if row else None
        finally:
            conn.close()

    def available_rep(self, exclude_rep_id: str | None = None) -> Rep | None:
        conn = self._db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM reps WHERE available=1 AND rep_id != ? LIMIT 1",
                (exclude_rep_id or "",),
            ).fetchone()
            return self._rep(row) if row else None
        finally:
            conn.close()

    # -- writes / audit trail ---------------------------------------------- #
    def _execute(self, sql: str, params: Iterable) -> None:
        conn = self._db.connect()
        try:
            conn.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    def start_call(self, call_id: str) -> None:
        self._execute(
            "INSERT OR REPLACE INTO calls (call_id, start_time) VALUES (?,?)",
            (call_id, _now()),
        )

    def log_offer(self, call_id: str, round_number: int, party: OfferParty,
                  amount: float) -> None:
        self._execute(
            "INSERT INTO negotiation_offers (call_id, round_number, offered_by, amount, timestamp)"
            " VALUES (?,?,?,?,?)",
            (call_id, round_number, party.value, amount, _now()),
        )

    def log_note(self, call_id: str, note: str) -> None:
        self._execute(
            "INSERT INTO call_notes (call_id, note, timestamp) VALUES (?,?,?)",
            (call_id, note, _now()),
        )

    def log_transfer(self, call_id: str, rep_id: str, result: str) -> None:
        self._execute(
            "INSERT INTO transfer_events (call_id, rep_id, transfer_result, timestamp)"
            " VALUES (?,?,?,?)",
            (call_id, rep_id, result, _now()),
        )

    def book_load(self, load_id: str) -> None:
        self._execute("UPDATE loads SET status='covered' WHERE load_id=?", (load_id,))

    def add_carrier_email(self, usdot_number: str, email: str) -> bool:
        """Append an address to the carrier's file. Returns True if it's new.

        Existing addresses are kept — a carrier can have dispatch, billing and a
        second office, so this accumulates rather than overwrites.
        """
        normalized = email.strip().lower()
        if self.email_on_file(usdot_number, normalized):
            return False
        self._execute(
            "INSERT OR IGNORE INTO carrier_emails (usdot_number, email, added_at)"
            " VALUES (?,?,?)",
            (usdot_number, normalized, _now()),
        )
        return True

    def end_call(self, call_id: str, load_id: str | None, carrier_dot: str | None,
                 outcome: str, transcript: list | str) -> None:
        payload = transcript if isinstance(transcript, str) else json.dumps(transcript)
        self._execute(
            "UPDATE calls SET load_id=?, carrier_dot=?, end_time=?, outcome=?, transcript=?"
            " WHERE call_id=?",
            (load_id, carrier_dot, _now(), outcome, payload, call_id),
        )
