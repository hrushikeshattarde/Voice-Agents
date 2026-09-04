"""
Read model for the dashboard — every SELECT the UI needs, in one place.

This reads the same SQLite file the agent writes its audit trail to
(`calls`, `negotiation_offers`, `call_notes`, `transfer_events`), plus the
loads/carriers tables that hold the seed board offline and the local copies
in live mode. It never writes during normal serving; the one mutation
(`reset_board`) exists so the offline playground can restore the seeded board
after test bookings cover it, and it deliberately leaves the call history alone.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

from lanevoice.db.database import Database

# The outcome vocabulary is CallOutcome's values. Fixed order so the UI's
# outcome colors follow the entity, never the rank in this week's data.
OUTCOMES = ("booked", "transferred", "no_deal", "rejected", "abandoned")


def _parse_ts(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_secs(start: str | None, end: str | None) -> float | None:
    t0, t1 = _parse_ts(start), _parse_ts(end)
    if t0 is None or t1 is None:
        return None
    return max((t1 - t0).total_seconds(), 0.0)


def _transcript_turns(payload: str | None) -> list[list[str]]:
    """The stored transcript as [speaker, line] pairs, or [] when unreadable.

    `end_call` stores `json.dumps(list_of_pairs)` but accepts any string, so a
    row that won't parse renders as an empty transcript rather than a 500 —
    the rest of the call record is still worth showing.
    """
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    turns = []
    for item in data:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            turns.append([str(item[0]), str(item[1])])
    return turns


class DashboardQueries:
    def __init__(self, db: Database):
        self._db = db

    # -- recordings ------------------------------------------------------------#
    def recording_path(self, call_id: str) -> Path | None:
        """The worker's saved call recording, if this call has one. File-backed
        next to the DB (`call_recordings/<call_id>.ogg`, see
        telephony.worker.save_call_recording) — no column to migrate, and a
        deleted file simply reads as "no recording"."""
        path = Path(self._db.path).parent / "call_recordings" / f"{call_id}.ogg"
        return path if path.is_file() else None

    # -- calls ---------------------------------------------------------------- #
    _CALL_SELECT = """
        SELECT c.call_id, c.load_id, c.carrier_dot, c.caller_number,
               c.start_time, c.end_time,
               c.outcome, c.transcript, c.end_label, c.end_reason,
               c.carrier_name   AS call_carrier_name,
               c.carrier_mc     AS call_carrier_mc,
               ca.legal_name    AS carrier_name,
               ca.mc_number     AS carrier_mc,
               l.origin         AS load_origin,
               l.destination    AS load_destination,
               (SELECT MAX(o.round_number) FROM negotiation_offers o
                 WHERE o.call_id = c.call_id)                       AS rounds,
               (SELECT o.amount FROM negotiation_offers o
                 WHERE o.call_id = c.call_id
                 ORDER BY o.id DESC LIMIT 1)                        AS last_offer,
               EXISTS(SELECT 1 FROM call_notes n
                 WHERE n.call_id = c.call_id
                   AND n.note LIKE 'Playground test call%')         AS is_playground
          FROM calls c
          LEFT JOIN carriers ca ON ca.usdot_number = c.carrier_dot
          LEFT JOIN loads    l  ON UPPER(l.load_id) = UPPER(c.load_id)
    """

    @staticmethod
    def _call_row(row: sqlite3.Row, with_transcript: bool = False) -> dict:
        transcript = _transcript_turns(row["transcript"])
        lane = None
        if row["load_origin"] and row["load_destination"]:
            lane = f"{row['load_origin']} → {row['load_destination']}"
        out = {
            "call_id": row["call_id"],
            "load_id": row["load_id"],
            "lane": lane,
            "caller_number": row["caller_number"],
            "carrier_dot": row["carrier_dot"],
            # `c.carrier_name`/`carrier_mc` is what was recorded on the CALL —
            # the only source in a live Transport Pro deployment, where carriers
            # live in Transport Pro and the local `carriers` table (joined below)
            # is only ever populated by the offline demo seed.
            "carrier_name": row["call_carrier_name"] or row["carrier_name"],
            "carrier_mc": row["call_carrier_mc"] or row["carrier_mc"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "duration_secs": _duration_secs(row["start_time"], row["end_time"]),
            "outcome": row["outcome"],
            # Why the call ended, in a rep's own words — see
            # `CarrierSalesAgent._call_label`/`_call_summary_line`. Null on a
            # call still in progress, and on anything finished before this
            # existed.
            "label": row["end_label"],
            "reason": row["end_reason"],
            "turns": len(transcript) or None,
            "rounds": row["rounds"],
            "final_rate": row["last_offer"],
            # Where the call came from. Keyed off the marker note the session
            # manager writes, so a dashboard test call can never masquerade as
            # a carrier who actually rang the desk.
            "source": "playground" if row["is_playground"] else "phone",
        }
        if with_transcript:
            out["transcript"] = transcript
        return out

    def calls(self, outcome: str | None = None, label: str | None = None,
              q: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """Newest first. `outcome` filters exactly ('incomplete' = no outcome
        yet — a dropped or still-open call); `label` filters the reason a call
        ended on (see `_call_row`'s "label" — "Rate too high", "Other", and so
        on) exactly; `q` matches call id, load id, carrier DOT/MC/name, or the
        caller's phone number."""
        sql, params = self._CALL_SELECT, []
        where = []
        if outcome == "incomplete":
            where.append("c.outcome IS NULL")
        elif outcome:
            where.append("c.outcome = ?")
            params.append(outcome)
        if label:
            where.append("c.end_label = ?")
            params.append(label)
        if q:
            where.append("(c.call_id LIKE ? OR c.load_id LIKE ? OR "
                         "c.carrier_dot LIKE ? OR c.caller_number LIKE ? OR "
                         "c.carrier_name LIKE ? OR c.carrier_mc LIKE ? OR "
                         "ca.legal_name LIKE ? OR ca.mc_number LIKE ?)")
            params.extend([f"%{q}%"] * 8)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.start_time DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        conn = self._db.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [self._call_row(r) for r in rows]
        finally:
            conn.close()

    def call_detail(self, call_id: str) -> dict | None:
        conn = self._db.connect()
        try:
            row = conn.execute(
                self._CALL_SELECT + " WHERE c.call_id = ?", (call_id,)
            ).fetchone()
            if not row:
                return None
            detail = self._call_row(row, with_transcript=True)
            detail["offers"] = [
                {"round": r["round_number"], "party": r["offered_by"],
                 "amount": r["amount"], "timestamp": r["timestamp"]}
                for r in conn.execute(
                    "SELECT * FROM negotiation_offers WHERE call_id=? ORDER BY id",
                    (call_id,)).fetchall()
            ]
            detail["notes"] = [
                {"note": r["note"], "timestamp": r["timestamp"]}
                for r in conn.execute(
                    "SELECT * FROM call_notes WHERE call_id=? ORDER BY id",
                    (call_id,)).fetchall()
            ]
            detail["transfers"] = [
                {"rep_id": r["rep_id"], "result": r["transfer_result"],
                 "timestamp": r["timestamp"]}
                for r in conn.execute(
                    "SELECT * FROM transfer_events WHERE call_id=? ORDER BY id",
                    (call_id,)).fetchall()
            ]
            load = None
            if detail["load_id"]:
                lrow = conn.execute(
                    "SELECT * FROM loads WHERE UPPER(load_id)=UPPER(?)",
                    (detail["load_id"],)).fetchone()
                load = dict(lrow) if lrow else None
            detail["load"] = load
            detail["has_recording"] = self.recording_path(call_id) is not None
            return detail
        finally:
            conn.close()

    # -- overview ------------------------------------------------------------- #
    def overview(self, days: int = 30) -> dict:
        conn = self._db.connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            by_outcome = dict(conn.execute(
                "SELECT outcome, COUNT(*) FROM calls WHERE outcome IS NOT NULL "
                "GROUP BY outcome").fetchall())
            completed = sum(by_outcome.values())
            outcomes = [{"outcome": o, "count": by_outcome.get(o, 0)}
                        for o in OUTCOMES]

            durations = [
                d for d in (
                    _duration_secs(r["start_time"], r["end_time"])
                    for r in conn.execute(
                        "SELECT start_time, end_time FROM calls "
                        "WHERE outcome IS NOT NULL").fetchall())
                if d is not None
            ]
            avg_duration = sum(durations) / len(durations) if durations else None

            rounds = [r[0] for r in conn.execute(
                "SELECT MAX(round_number) FROM negotiation_offers GROUP BY call_id"
            ).fetchall() if r[0] is not None]
            avg_rounds = sum(rounds) / len(rounds) if rounds else None

            booked_rates = [r[0] for r in conn.execute(
                """SELECT (SELECT o.amount FROM negotiation_offers o
                            WHERE o.call_id = c.call_id
                            ORDER BY o.id DESC LIMIT 1)
                     FROM calls c WHERE c.outcome = 'booked'""").fetchall()
                if r[0] is not None]

            # Fixed window ending today, empty days filled — a gap is data.
            today = datetime.date.today()
            first = today - datetime.timedelta(days=days - 1)
            per_day = {r[0]: (r[1], r[2]) for r in conn.execute(
                """SELECT substr(start_time, 1, 10) AS day, COUNT(*),
                          SUM(CASE WHEN outcome='booked' THEN 1 ELSE 0 END)
                     FROM calls WHERE start_time >= ? GROUP BY day""",
                (first.isoformat(),)).fetchall()}
            calls_by_day = []
            for i in range(days):
                day = (first + datetime.timedelta(days=i)).isoformat()
                count, booked = per_day.get(day, (0, 0))
                calls_by_day.append({"day": day, "calls": count,
                                     "booked": booked or 0})
        finally:
            conn.close()

        return {
            "kpis": {
                "total_calls": total,
                "completed": completed,
                "booked": by_outcome.get("booked", 0),
                "transferred": by_outcome.get("transferred", 0),
                "no_deal": by_outcome.get("no_deal", 0),
                "booking_rate": (by_outcome.get("booked", 0) / completed
                                 if completed else None),
                "avg_duration_secs": avg_duration,
                "avg_rounds": avg_rounds,
                "booked_value": sum(booked_rates) if booked_rates else 0,
                "avg_booked_rate": (sum(booked_rates) / len(booked_rates)
                                    if booked_rates else None),
            },
            "outcomes": outcomes,
            "calls_by_day": calls_by_day,
            "recent": self.calls(limit=6),
        }

    # -- loads ---------------------------------------------------------------- #
    def loads(self) -> list[dict]:
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM loads ORDER BY is_posted DESC, "
                "CASE status WHEN 'open' THEN 0 ELSE 1 END, pickup_date"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- playground board reset ------------------------------------------------ #
    def reset_board(self) -> None:
        """Re-seed loads/carriers/reps to their pristine state, keeping every
        call record. Offline convenience only: after a playground booking covers
        L1001, this puts it back on the board without erasing the run history
        (which `Database.reset` would)."""
        conn = self._db.connect()
        try:
            for table in ("loads", "carriers", "carrier_emails", "reps"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
            conn.commit()
        finally:
            conn.close()
        from lanevoice.db.seed import seed_if_empty
        seed_if_empty(self._db)
