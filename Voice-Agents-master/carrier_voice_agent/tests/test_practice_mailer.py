"""The manager email: the roster file, the message, the send, the record.

No test here touches a network — the SMTP conversation runs against a fake
that records the protocol calls in order, because what would break in
production is exactly that order (STARTTLS before login, login before send)
and the non-fatality contract: a dead mail server must cost the manager an
email and leave a recorded error, never cost the rep their scorecard or their
end-of-call response. The roster tests hold managers.toml to the same standard
as the profiles: refuse a malformed entry at boot with the field named — the
alternative is a quarter of reports silently mailed to a typo.
"""

from __future__ import annotations

import json

import pytest

from lanevoice.db.database import Database
from lanevoice.practice.judge import FOCUS_KEY, RUBRIC
from lanevoice.practice.mailer import ReportMailer, build_report_email
from lanevoice.practice.managers import load_managers
from lanevoice.practice.sessions import PracticeSessionManager
from lanevoice.practice.store import PracticeStore
from lanevoice.settings import get_settings

MANAGERS_TOML = """
[[managers]]
id = "asmith"
name = "Alex Smith"
email = "Alex.Smith@example.com"

[[managers]]
id = "jdoe"
name = "Jordan Doe"
email = "jordan.doe@example.com"
"""


def _report(**overrides) -> dict:
    scores = {k: {"score": 7, "quote": "q", "comment": f"{k} comment"}
              for k in [*RUBRIC, FOCUS_KEY]}
    report = {"overall": 7.0, "scores": scores, "win_condition_met": True,
              "win_evidence": "callback agreed",
              "strengths": ["sharp opener"],
              "improvements": [{"what": "dig deeper", "why": "left pain unfound",
                                "quote": "we're covered",
                                "better_line": "How's he on Friday nights?"}],
              "summary": "Good call.",
              "metrics": {"rep_turns": 3, "talk_ratio": 0.6, "questions": 2,
                          "wpm": 150, "fillers_per_min": 1.0}}
    report.update(overrides)
    return report


# ------------------------------------------------------------------- roster #
def test_the_roster_loads_and_normalises_emails(tmp_path):
    path = tmp_path / "managers.toml"
    path.write_text(MANAGERS_TOML, encoding="utf-8")
    managers = load_managers(path)
    assert set(managers) == {"asmith", "jdoe"}
    assert managers["asmith"].email == "alex.smith@example.com"   # lowercased


def test_a_missing_roster_is_an_empty_desk_not_a_crash(tmp_path):
    assert load_managers(tmp_path / "nope.toml") == {}


def test_a_typo_email_is_refused_with_the_file_named(tmp_path):
    path = tmp_path / "managers.toml"
    path.write_text('[[managers]]\nid = "x"\nname = "X"\nemail = "not-an-email"',
                    encoding="utf-8")
    with pytest.raises(ValueError, match=r"managers\.toml.*not-an-email"):
        load_managers(path)


def test_duplicate_manager_ids_are_refused(tmp_path):
    path = tmp_path / "managers.toml"
    path.write_text(MANAGERS_TOML.replace("jdoe", "asmith"), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate manager id"):
        load_managers(path)


def test_the_shipped_roster_file_parses():
    # Ships with examples commented out — an empty roster, never a broken one.
    assert load_managers() == {}


# ------------------------------------------------------------------ message #
def test_the_email_carries_both_verdicts_and_the_coaching():
    report = _report(delivery={"overall": 6.4, "scores": {
        "warmth": {"score": 5, "comment": "transactional"}},
        "coaching": ["vary your pitch"]})
    msg = build_report_email(rep_name="Jordan", profile_name="The Brush-off",
                             manager_name="Alex Smith",
                             manager_email="alex.smith@example.com",
                             report=report, mode="voice",
                             sender="lanevoice@example.com")
    assert msg["Subject"] == "Practice report — Jordan vs The Brush-off — 7.0/10"
    assert msg["To"] == "Alex Smith <alex.smith@example.com>"
    assert msg["From"] == "lanevoice@example.com"
    text = msg.get_body(("plain",)).get_content()
    assert "goal ACHIEVED" in text
    assert "Objection handling" in text
    assert "Voice delivery: 6.4/10" in text
    assert "vary your pitch" in text
    assert 'try:  "How\'s he on Friday nights?"' in text
    assert "call recording" in text                # voice sessions point at it
    html = msg.get_body(("html",)).get_content()
    assert "Voice delivery" in html


def test_a_judge_error_report_still_makes_an_honest_email():
    msg = build_report_email(rep_name="Jordan", profile_name="The Brush-off",
                             manager_name="Alex", manager_email="a@example.com",
                             report={"judge_error": "model down",
                                     "metrics": {"rep_turns": 2}},
                             mode="text", sender="lv@example.com")
    assert "unscored" in msg["Subject"]
    assert "judge failed" in msg.get_body(("plain",)).get_content()


# --------------------------------------------------------------------- send #
class _FakeSMTP:
    """Records the protocol conversation; injected as the smtp_factory."""

    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[tuple] = []
        self.fail_on_send = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        if self.fail_on_send:
            raise ConnectionResetError("mail server hung up")
        self.calls.append(("send", msg["To"]))


def _smtp_settings(**overrides):
    return get_settings().model_copy(update={
        "smtp_host": "mail.example.com", "smtp_port": 2525,
        "smtp_username": "lv", "smtp_password": "secret",
        "smtp_from": "lanevoice@example.com", **overrides})


def test_the_smtp_conversation_runs_in_protocol_order():
    _FakeSMTP.instances.clear()
    mailer = ReportMailer(_smtp_settings(), smtp_factory=_FakeSMTP)
    msg = build_report_email(rep_name="J", profile_name="P", manager_name="A",
                             manager_email="a@example.com", report=_report(),
                             mode="text", sender="lanevoice@example.com")
    mailer.send(msg)
    smtp = _FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port) == ("mail.example.com", 2525)
    assert [c[0] for c in smtp.calls] == ["starttls", "login", "send", "quit"]


def test_starttls_is_skipped_when_turned_off():
    _FakeSMTP.instances.clear()
    ReportMailer(_smtp_settings(smtp_starttls=False, smtp_username=""),
                 smtp_factory=_FakeSMTP).send(
        build_report_email(rep_name="J", profile_name="P", manager_name="A",
                           manager_email="a@example.com", report=_report(),
                           mode="text", sender="s@example.com"))
    assert [c[0] for c in _FakeSMTP.instances[-1].calls] == ["send", "quit"]


def test_the_mailer_refuses_to_build_unconfigured():
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        ReportMailer(get_settings().model_copy(update={"smtp_host": "",
                                                       "smtp_from": ""}))


# ------------------------------------------------------------------ manager #
def _script(*lines):
    queue = list(lines)

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        assert queue, "the customer script ran out of lines"
        return queue.pop(0)

    return chat


def _fake_judge(settings):
    def chat(system: str, user: str, *, max_tokens: int) -> str:
        return json.dumps({"scores": {k: {"score": 7, "quote": "q", "comment": "c"}
                                      for k in [*RUBRIC, FOCUS_KEY]},
                           "win_condition_met": True, "win_evidence": "",
                           "strengths": [], "improvements": [], "summary": "ok"})

    return chat


class _FakeMailer:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list = []

    def send(self, msg):
        if self.fail:
            raise ConnectionResetError("mail server hung up")
        self.sent.append(msg)


def _manager(tmp_path, *, smtp: bool = True, fail_send: bool = False):
    roster = tmp_path / "managers.toml"
    roster.write_text(MANAGERS_TOML, encoding="utf-8")
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    settings = _smtp_settings() if smtp else get_settings()
    fake_mailer = _FakeMailer(fail=fail_send)
    mgr = PracticeSessionManager(
        db, settings, chat_factory=lambda s: _script("We're covered."),
        judge_factory=_fake_judge, managers_path=roster,
        mailer_factory=lambda s: fake_mailer)
    return mgr, PracticeStore(db), fake_mailer


def test_a_scored_session_with_a_manager_is_emailed_and_recorded(tmp_path):
    mgr, store, fake_mailer = _manager(tmp_path)
    started = mgr.start("brush_off", "Jordan", manager_id="asmith")
    assert started["manager"]["name"] == "Alex Smith"
    mgr.turn(started["session_id"], "Quick question —")
    summary = mgr.end(started["session_id"])
    assert summary["report"]["email_status"]["emailed_to"] == "alex.smith@example.com"
    assert len(fake_mailer.sent) == 1
    assert "Jordan vs The Brush-off" in fake_mailer.sent[0]["Subject"]
    detail = store.report_detail(started["session_id"])
    assert detail["report"]["emailed_to"] == "alex.smith@example.com"
    assert detail["report"]["emailed_at"]
    assert detail["report"]["email_error"] is None


def test_a_dead_mail_server_is_recorded_never_raised(tmp_path):
    mgr, store, _ = _manager(tmp_path, fail_send=True)
    started = mgr.start("brush_off", "Jordan", manager_id="asmith")
    mgr.turn(started["session_id"], "Quick question —")
    summary = mgr.end(started["session_id"])          # must not raise
    assert "hung up" in summary["report"]["email_status"]["error"]
    assert summary["report"]["overall"] == 7.0        # the scorecard survived
    detail = store.report_detail(started["session_id"])
    assert "hung up" in detail["report"]["email_error"]
    assert detail["report"]["emailed_to"] is None


def test_unconfigured_smtp_records_the_fix_instead_of_sending(tmp_path):
    mgr, store, fake_mailer = _manager(tmp_path, smtp=False)
    started = mgr.start("brush_off", "Jordan", manager_id="jdoe")
    mgr.turn(started["session_id"], "Quick question —")
    summary = mgr.end(started["session_id"])
    assert "SMTP_HOST" in summary["report"]["email_status"]["error"]
    assert fake_mailer.sent == []
    assert "SMTP_HOST" in store.report_detail(started["session_id"])["report"]["email_error"]


def test_no_manager_means_no_email_and_no_noise(tmp_path):
    mgr, store, fake_mailer = _manager(tmp_path)
    started = mgr.start("brush_off", "Jordan")
    mgr.turn(started["session_id"], "Quick question —")
    summary = mgr.end(started["session_id"])
    assert "email_status" not in summary["report"]
    assert fake_mailer.sent == []
    detail = store.report_detail(started["session_id"])
    assert detail["report"]["emailed_to"] is None
    assert detail["report"]["email_error"] is None


def test_an_unknown_manager_id_is_refused_by_name(tmp_path):
    mgr, _, _ = _manager(tmp_path)
    with pytest.raises(ValueError, match="nobody"):
        mgr.start("brush_off", "Jordan", manager_id="nobody")
