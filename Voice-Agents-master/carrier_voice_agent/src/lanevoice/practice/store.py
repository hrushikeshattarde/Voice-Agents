"""
Persistence for practice sessions — written turn by turn, like the phone worker
writes calls: a browser tab closed mid-session loses nothing but the turn in
flight, and the transcript is already on disk for the phase-2 scoring pass.
"""

from __future__ import annotations

import json

from lanevoice.db.database import Database


class PracticeStore:
    """The practice tables — `practice_sessions` and `practice_reports` — and
    nothing else."""

    def __init__(self, db: Database):
        self._db = db

    def start(self, session_id: str, profile_id: str, profile_name: str,
              rep_name: str, transcript: list[list[str]], mode: str,
              manager_name: str | None = None,
              manager_email: str | None = None) -> None:
        conn = self._db.connect()
        try:
            conn.execute(
                """INSERT INTO practice_sessions
                       (session_id, rep_name, profile_id, profile_name,
                        started_at, turns, transcript, status, mode,
                        manager_name, manager_email)
                   VALUES (?,?,?,?,datetime('now'),0,?,'active',?,?,?)""",
                (session_id, rep_name, profile_id, profile_name,
                 json.dumps(transcript), mode, manager_name, manager_email))
            conn.commit()
        finally:
            conn.close()

    def record(self, session_id: str, transcript: list[list[str]], turns: int,
               rep_audio_secs: float, customer_audio_secs: float) -> None:
        """One exchange landed — persist the whole transcript again. Sessions are
        short and SQLite is local, so rewriting beats an append scheme that could
        interleave under the server's thread-per-request model."""
        conn = self._db.connect()
        try:
            conn.execute(
                """UPDATE practice_sessions
                      SET transcript = ?, turns = ?,
                          rep_audio_secs = ?, customer_audio_secs = ?
                    WHERE session_id = ?""",
                (json.dumps(transcript), turns,
                 round(rep_audio_secs, 2), round(customer_audio_secs, 2),
                 session_id))
            conn.commit()
        finally:
            conn.close()

    def finish(self, session_id: str, end_reason: str, transcript: list[list[str]],
               turns: int, rep_audio_secs: float, customer_audio_secs: float) -> None:
        conn = self._db.connect()
        try:
            conn.execute(
                """UPDATE practice_sessions
                      SET transcript = ?, turns = ?, ended_at = datetime('now'),
                          status = 'done', end_reason = ?,
                          rep_audio_secs = ?, customer_audio_secs = ?
                    WHERE session_id = ?""",
                (json.dumps(transcript), turns, end_reason,
                 round(rep_audio_secs, 2), round(customer_audio_secs, 2),
                 session_id))
            conn.commit()
        finally:
            conn.close()

    def session(self, session_id: str) -> dict | None:
        conn = self._db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM practice_sessions WHERE session_id = ?",
                (session_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        record = dict(row)
        record["transcript"] = json.loads(record["transcript"] or "[]")
        return record

    # -- reports ---------------------------------------------------------------#
    def save_report(self, session_id: str, report: dict) -> None:
        """REPLACE on purpose: re-scoring a session updates its one report."""
        conn = self._db.connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO practice_reports
                       (session_id, overall, win_condition_met, scores_json,
                        strengths_json, improvements_json, metrics_json, summary,
                        judge_error, judge_model, created_at, delivery_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),?)""",
                (session_id,
                 report.get("overall"),
                 int(bool(report.get("win_condition_met"))),
                 json.dumps(report.get("scores") or {}),
                 json.dumps(report.get("strengths") or []),
                 json.dumps(report.get("improvements") or []),
                 json.dumps(report.get("metrics") or {}),
                 report.get("summary"),
                 report.get("judge_error"),
                 report.get("judge_model"),
                 json.dumps(report["delivery"]) if report.get("delivery") else None))
            conn.commit()
        finally:
            conn.close()

    def record_email(self, session_id: str, emailed_to: str | None = None,
                     error: str | None = None) -> None:
        """How the manager email actually went — exactly one of the two."""
        conn = self._db.connect()
        try:
            if emailed_to:
                conn.execute(
                    """UPDATE practice_reports
                          SET emailed_to = ?, emailed_at = datetime('now'),
                              email_error = NULL
                        WHERE session_id = ?""", (emailed_to, session_id))
            else:
                conn.execute(
                    "UPDATE practice_reports SET email_error = ? WHERE session_id = ?",
                    (error, session_id))
            conn.commit()
        finally:
            conn.close()

    def reports(self, rep: str | None = None, limit: int = 50,
                offset: int = 0) -> list[dict]:
        """Finished sessions newest-first, each with its scorecard headline (or
        NULLs where the judge never ran — those rows still show, because a
        session that failed to score is a fact worth seeing, not hiding)."""
        query = """
            SELECT s.session_id, s.rep_name, s.profile_id, s.profile_name, s.mode,
                   s.started_at, s.ended_at, s.turns, s.end_reason,
                   r.overall, r.win_condition_met, r.judge_error
              FROM practice_sessions s
              LEFT JOIN practice_reports r USING (session_id)
             WHERE s.status = 'done'"""
        params: list = []
        if rep:
            query += " AND s.rep_name LIKE ?"
            params.append(f"%{rep}%")
        # rowid breaks ties: `datetime('now')` has one-second granularity, and
        # two sessions in the same second must still list newest-first.
        query += " ORDER BY s.started_at DESC, s.rowid DESC LIMIT ? OFFSET ?"
        params += [max(1, min(int(limit), 200)), max(0, int(offset))]
        conn = self._db.connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            record = dict(row)
            record["win_condition_met"] = (None if record["win_condition_met"] is None
                                           else bool(record["win_condition_met"]))
            out.append(record)
        return out

    def report_detail(self, session_id: str) -> dict | None:
        """The whole story of one session: row, transcript, and scorecard."""
        record = self.session(session_id)
        if record is None:
            return None
        conn = self._db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM practice_reports WHERE session_id = ?",
                (session_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            record["report"] = None
            return record
        report = dict(row)
        for key in ("scores_json", "strengths_json", "improvements_json",
                    "metrics_json", "delivery_json"):
            report[key.removesuffix("_json")] = json.loads(report.pop(key) or "null")
        report["win_condition_met"] = bool(report["win_condition_met"])
        record["report"] = report
        return record
