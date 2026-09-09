"""The post-call summary email: the same CALL SUMMARY note the load gets in
Transport Pro, mailed straight to whoever that load's carrier-sales rep is —
so the desk hears about a call without opening the dashboard. Driven through
REAL calls to a real outcome, the same way test_call_summary.py insists on:
the label in the subject line has to be right for the code path that actually
produces it, not just for a hand-built call object. No test here touches a
network — a fake mailer, injected the same seam `PracticeSessionManager`
already uses for its own report email, records what would have gone out.
"""

import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.reps import load_reps
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer

EMPTY = "empty in Dallas, Texas today"
ON_FILE = "dispatch@blueskylogistics.com"


def _give_rep_an_email(repo, rep_id, email):
    conn = repo._db.connect()
    try:
        conn.execute("UPDATE reps SET email=? WHERE rep_id=?", (email, rep_id))
        conn.commit()
    finally:
        conn.close()


def _call_row(repo, call_id):
    conn = repo._db.connect()
    try:
        row = conn.execute(
            "SELECT rep_email_sent_to, rep_email_sent_at, rep_email_error "
            "FROM calls WHERE call_id=?", (call_id,)).fetchone()
    finally:
        conn.close()
    return dict(row)


class _FakeMailer:
    """Records what would have gone out; injected via `mailer_factory` so no
    test here opens a real SMTP connection."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list = []

    def send(self, msg):
        if self.fail:
            raise ConnectionResetError("mail server hung up")
        self.sent.append(msg)


def _smtp_settings(**overrides):
    return get_settings().model_copy(update={
        "smtp_host": "mail.example.com", "smtp_from": "lanevoice@example.com",
        **overrides})


def _agent(repo, fake_mailer, settings=None):
    return CarrierSalesAgent(repo, StubComposer(), settings=settings or _smtp_settings(),
                             mailer_factory=lambda s: fake_mailer)


def _book_l1001(a):
    """L1001's assigned rep is seeded as R01 (Sarah Chen) — see db/seed.py."""
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 123456")                        # Blue Sky Logistics — active
    a.handle(EMPTY)
    a.handle("yeah that works")
    a.handle("yep, I can cover it")
    a.handle(ON_FILE)


def test_a_booked_call_emails_the_loads_assigned_rep(repo):
    _give_rep_an_email(repo, "R01", "sarah.chen@example.com")
    fake_mailer = _FakeMailer()
    a = _agent(repo, fake_mailer)
    _book_l1001(a)
    a.abandon()                                   # the worker always calls this at hang-up
    assert a.summary()["outcome"] == "booked"

    (msg,) = fake_mailer.sent
    assert msg["To"] == "Sarah Chen <sarah.chen@example.com>"
    assert msg["From"] == "lanevoice@example.com"
    assert "Load L1001" in msg["Subject"] and "Success" in msg["Subject"]
    body = msg.get_content()
    assert "CALL SUMMARY" in body and "Label: Success" in body

    row = _call_row(repo, a.call_id)
    assert row["rep_email_sent_to"] == "sarah.chen@example.com"
    assert row["rep_email_sent_at"]
    assert row["rep_email_error"] is None


def test_a_rejected_call_carries_its_own_label_not_a_hardcoded_one(repo):
    _give_rep_an_email(repo, "R01", "sarah.chen@example.com")
    fake_mailer = _FakeMailer()
    a = _agent(repo, fake_mailer)
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 555444")                        # Dormant Transport — inactive
    a.handle("yeah, that's us")                  # the name read back is confirmed first
    a.abandon()
    assert a.summary()["outcome"] == "rejected"

    (msg,) = fake_mailer.sent
    assert "Carrier not qualified" in msg["Subject"]


def test_a_rep_with_no_email_on_file_sends_nothing(repo):
    # R01 keeps the seed data's default here: no email on file for anyone.
    fake_mailer = _FakeMailer()
    a = _agent(repo, fake_mailer)
    _book_l1001(a)
    a.abandon()
    assert fake_mailer.sent == []
    row = _call_row(repo, a.call_id)
    assert row["rep_email_sent_to"] is None
    assert row["rep_email_error"] is None          # no address on file is not an error


def test_a_call_that_never_resolves_a_load_sends_nothing(repo):
    _give_rep_an_email(repo, "R01", "sarah.chen@example.com")
    fake_mailer = _FakeMailer()
    a = _agent(repo, fake_mailer)
    a.greeting()
    a.abandon()                                   # hung up before naming any load
    assert fake_mailer.sent == []


def test_unconfigured_smtp_records_the_fix_instead_of_sending(repo):
    _give_rep_an_email(repo, "R01", "sarah.chen@example.com")
    fake_mailer = _FakeMailer()
    unconfigured = get_settings().model_copy(update={"smtp_host": "", "smtp_from": ""})
    a = _agent(repo, fake_mailer, settings=unconfigured)
    _book_l1001(a)
    a.abandon()
    assert fake_mailer.sent == []

    row = _call_row(repo, a.call_id)
    assert row["rep_email_sent_to"] is None
    assert "SMTP_HOST" in row["rep_email_error"]


def test_a_dead_mail_server_is_recorded_never_raised(repo):
    _give_rep_an_email(repo, "R01", "sarah.chen@example.com")
    fake_mailer = _FakeMailer(fail=True)
    a = _agent(repo, fake_mailer)
    _book_l1001(a)
    a.abandon()                                   # must not raise
    assert a.summary()["outcome"] == "booked"      # the booking itself is unaffected

    row = _call_row(repo, a.call_id)
    assert row["rep_email_sent_to"] is None
    assert "hung up" in row["rep_email_error"]


# --------------------------------------------------------------------------- #
# reps.toml's `email` field: same optional-override, same validation posture
# as everything else in the file (a bad entry is refused at load, named).
# --------------------------------------------------------------------------- #
def test_the_directory_accepts_and_normalises_an_optional_email(tmp_path):
    path = tmp_path / "reps.toml"
    path.write_text(
        '[[reps]]\nid = "jsmith"\nname = "Jordan Smith"\nphone = "+12605551234"\n'
        'email = "Jordan.Smith@Example.com"\n', encoding="utf-8")
    (rep,) = load_reps(path)
    assert rep.email == "jordan.smith@example.com"


def test_email_stays_optional(tmp_path):
    path = tmp_path / "reps.toml"
    path.write_text(
        '[[reps]]\nid = "jsmith"\nname = "Jordan Smith"\nphone = "+12605551234"\n',
        encoding="utf-8")
    (rep,) = load_reps(path)
    assert rep.email is None


def test_a_typo_email_is_refused_with_the_file_named(tmp_path):
    path = tmp_path / "reps.toml"
    path.write_text(
        '[[reps]]\nid = "jsmith"\nname = "Jordan Smith"\nphone = "+12605551234"\n'
        'email = "not-an-email"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"reps\.toml.*not-an-email"):
        load_reps(path)

