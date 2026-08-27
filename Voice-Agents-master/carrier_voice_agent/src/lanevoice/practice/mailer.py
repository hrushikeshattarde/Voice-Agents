"""
The report email — the scorecard, delivered to the rep's account manager.

This is the codebase's first outbound mail, and it stays deliberately small:
stdlib `smtplib` + `EmailMessage`, no provider SDK, no queue. One message per
scored session, sent inline at scoring time and NON-FATALLY — a dead mail
server costs the manager an email (recorded on the report row), never the rep
their scorecard.

The body is built from the same normalized report dict the dashboard renders,
as plain text with an HTML alternative, so it reads in anything from Outlook
to `mail`. It carries both verdicts — the conversational rubric and, for voice
sessions, the vocal delivery — plus the metrics, and points the manager at the
dashboard for the transcript and the call recording.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from html import escape

from lanevoice.practice.judge import RUBRIC
from lanevoice.settings import Settings, get_settings

# Presentation names for the stored dimension keys — keep in step with app.js.
_DIM_LABELS = {
    "opening": "Opening & hook", "discovery": "Discovery", "listening": "Listening",
    "objection_handling": "Objection handling", "value": "Value proposition",
    "composure": "Composure", "closing": "Closing & next step",
    "focus": "Persona focus",
}
_DELIVERY_LABELS = {"confidence": "Confidence", "clarity": "Clarity",
                    "energy": "Energy", "pace": "Pace", "warmth": "Warmth"}

_SEND_TIMEOUT = 15.0


def build_report_email(*, rep_name: str, profile_name: str, manager_name: str,
                       manager_email: str, report: dict, mode: str,
                       sender: str) -> EmailMessage:
    overall = report.get("overall")
    subject_score = "unscored" if overall is None else f"{overall}/10"
    msg = EmailMessage()
    msg["Subject"] = f"Practice report — {rep_name} vs {profile_name} — {subject_score}"
    msg["From"] = sender
    msg["To"] = f"{manager_name} <{manager_email}>"
    text = _text_body(rep_name, profile_name, report, mode)
    msg.set_content(text)
    msg.add_alternative(_html_body(text), subtype="html")
    return msg


def _text_body(rep_name: str, profile_name: str, report: dict, mode: str) -> str:
    lines = [
        f"Practice session report — {rep_name}",
        f"Customer profile: {profile_name} ({'voice' if mode == 'voice' else 'text'} session)",
        "",
    ]
    if report.get("judge_error"):
        lines += ["The scoring judge failed on this session; the transcript is "
                  "saved in the dashboard and the session can be re-run.", ""]
    else:
        overall = report.get("overall")
        won = report.get("win_condition_met")
        lines.append(f"Overall: {overall}/10 — goal "
                     f"{'ACHIEVED' if won else 'not reached'}")
        if report.get("win_evidence"):
            lines.append(f"  ({report['win_evidence']})")
        lines.append("")
        lines.append("Conversation scores:")
        scores = report.get("scores") or {}
        for key in [*RUBRIC, "focus"]:
            entry = scores.get(key)
            if entry:
                score = "—" if entry.get("score") is None else entry["score"]
                lines.append(f"  {_DIM_LABELS[key]:<22} {score:>2}  {entry.get('comment', '')}")
        lines.append("")
        delivery = report.get("delivery") or {}
        if delivery and not delivery.get("delivery_error"):
            lines.append(f"Voice delivery: {delivery.get('overall')}/10")
            for key, label in _DELIVERY_LABELS.items():
                entry = (delivery.get("scores") or {}).get(key)
                if entry:
                    score = "—" if entry.get("score") is None else entry["score"]
                    lines.append(f"  {label:<22} {score:>2}  {entry.get('comment', '')}")
            for coach in delivery.get("coaching") or []:
                lines.append(f"  Vocal coaching: {coach}")
            lines.append("")
        if report.get("strengths"):
            lines.append("Keep doing:")
            lines += [f"  - {s}" for s in report["strengths"]]
            lines.append("")
        if report.get("improvements"):
            lines.append("Work on:")
            for imp in report["improvements"]:
                lines.append(f"  - {imp.get('what', '')}")
                if imp.get("quote"):
                    lines.append(f"      said: \"{imp['quote']}\"")
                if imp.get("better_line"):
                    lines.append(f"      try:  \"{imp['better_line']}\"")
            lines.append("")
        if report.get("summary"):
            lines += [f"Coach's summary: {report['summary']}", ""]
    metrics = report.get("metrics") or {}
    if metrics:
        parts = [f"turns {metrics.get('rep_turns')}",
                 f"talk ratio {metrics.get('talk_ratio')}",
                 f"questions {metrics.get('questions')}"]
        if metrics.get("wpm"):
            parts.append(f"pace {metrics['wpm']} wpm")
        if metrics.get("fillers_per_min") is not None:
            parts.append(f"fillers/min {metrics['fillers_per_min']}")
        lines.append("Metrics: " + ", ".join(str(p) for p in parts))
        lines.append("")
    lines.append("Transcript"
                 + (" and call recording" if mode == "voice" else "")
                 + " are in the LaneVoice dashboard under Practice > Recent sessions.")
    return "\n".join(lines)


def _html_body(text: str) -> str:
    """The plain text, made mail-client-proof rather than art-directed: a <pre>
    keeps the aligned score columns aligned in every client ever written."""
    return ("<html><body><pre style=\"font-family: ui-monospace, Consolas, "
            "monospace; font-size: 13px; line-height: 1.5;\">"
            f"{escape(text)}</pre></body></html>")


class ReportMailer:
    """One SMTP conversation per report. `smtp_factory` is the test seam —
    production uses smtplib.SMTP itself."""

    def __init__(self, settings: Settings | None = None,
                 smtp_factory: Callable[..., smtplib.SMTP] | None = None):
        self._settings = settings or get_settings()
        if not self._settings.uses_practice_email:
            raise RuntimeError("Report email is not configured: set SMTP_HOST "
                               "and SMTP_FROM (see settings.py).")
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, msg: EmailMessage) -> None:
        """Raises on any SMTP failure — the session manager records it on the
        report row; nothing above that should ever see the exception."""
        settings = self._settings
        with self._smtp_factory(settings.smtp_host, settings.smtp_port,
                                timeout=_SEND_TIMEOUT) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
