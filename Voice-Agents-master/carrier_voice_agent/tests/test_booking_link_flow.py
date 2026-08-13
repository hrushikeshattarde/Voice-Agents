"""
The BOOKING state when a real booking link is available — whole calls.

This is where the honesty lives. With a link, the load is NOT the carrier's until
they open it and sign, so the agent must not say "booked" — and it must not try to
read a URL down a phone either. Both are pinned here, on the directive the state
machine authorised rather than on prose, because the composer is a stub.

The failure paths matter as much as the success. By the time this state runs the
carrier has already agreed a rate, so every outcome has to leave the call
somewhere coherent AND leave the audit trail able to tell a rep whether the rate
landed. A rep who guesses wrong either double-sells the lane or never places it.
"""

import json

import httpx
import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.voice import StubComposer
from tests.transportpro_fake import (
    HAPPYROBOT_URL,
    FakeTransportPro,
    board,
    repository,
    settings,
)
from tests.transportpro_payloads import (
    CARRIER_STATUS_ACTIVE,
    CONTACT_SEARCH,
    record_for,
)

LOAD = "1303369"
ON_ACCOUNT = "dispatch@blueskylogistics.com"
EMPTY = "empty in Nashville, Tennessee today"
BOOK_URL = "https://cli.transportpro.net/book/abc123"

_HR = {"happyrobot_url": HAPPYROBOT_URL, "happyrobot_token": "hr-token"}


@pytest.fixture
def fake():
    """The same board as the main flow tests, plus the two booking-link routes."""
    server = FakeTransportPro()
    board(server, record_for(int(LOAD)))
    server.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    server.json("/contact/search", CONTACT_SEARCH)
    server.json(f"/voiceai/load/{LOAD}/add_note", {})
    server.json("/offer", {"STATUS": "SUCCESS", "result": {"id": 99001}})
    server.json("/svc/happyrobot.php", {"book_now_url": BOOK_URL})
    return server


def _agent(fake, repo, **overrides):
    config = settings(max_negotiation_rounds=6, **(_HR | overrides))
    return CarrierSalesAgent(repository(fake, repo, **(_HR | overrides)),
                             StubComposer(), settings=config)


def _to_email(fake, repo, **overrides):
    agent = _agent(fake, repo, **overrides)
    agent.greeting()
    agent.handle(f"calling about load {LOAD}")
    agent.handle("MC 123456")
    agent.handle(EMPTY)
    # The pitch and the requirements are separate turns now — read together
    # they ran to 25 seconds of speech and hit the token limit mid-sentence.
    agent.handle("go ahead")                # -> the requirements get read
    agent.handle("yeah we can do that")     # -> and confirmed
    agent.handle("yeah that works")
    agent.handle("yep, we can cover that pickup")
    assert agent.state.value == "confirm_email"
    return agent


def _turns(agent):
    return agent._composer.turns


def _notes(repo):
    conn = repo._db.connect()
    try:
        return " ".join(r["note"] for r in
                        conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()


def _hr_bodies(fake):
    return [json.loads(r.content) for r in fake.calls("happyrobot.php")]


def _hr_actions(fake):
    """Which actions were invoked. `carrier_lookup` fires during carrier vetting
    on every call, so assertions about booking have to name `accept_offer`."""
    return [b["action"] for b in _hr_bodies(fake)]


# --------------------------------------------------------------------------- #
# The link path
# --------------------------------------------------------------------------- #
def test_agreeing_produces_a_link_and_records_the_offer(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "booked"
    assert agent.summary()["booking_link_sent"] is True
    assert agent._booking_link == BOOK_URL

    # The rate went on as a real offer, typed the way /offer demands...
    offer = json.loads(fake.calls("/offer")[0].content)
    assert offer["loadId"] == int(LOAD)
    assert offer["amount"] == 1600
    assert offer["email"] == ON_ACCOUNT
    assert offer["recordAsUserId"] == 4876
    # ...and exactly one accept, against that offer.
    accepts = [b for b in _hr_bodies(fake) if b["action"] == "accept_offer"]
    assert len(accepts) == 1
    assert accepts[0]["data"]["offer_id"] == 99001


def test_the_carrier_is_never_told_they_are_booked(fake, repo):
    """The whole reason this path exists. The load stays on the board until the
    link is completed, so "you're booked" is false at the moment it is said."""
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    directive = _turns(agent)[-1]["directive"].lower()

    # The log-only path's wording must NOT be what got authorised here.
    assert "confirm they're booked" not in directive
    # "booked" appears only inside the prohibition, which has to be explicit —
    # the model will otherwise reach for it, since every other booking-shaped
    # conversation ends that way.
    assert "do not tell them they are 'booked' or 'confirmed'" in directive
    # And it says what IS true: sign it, and it isn't theirs until you do.
    assert "sign" in directive
    assert "not theirs until they finish it" in directive


def test_the_url_is_never_given_to_the_model(fake, repo):
    """A URL cannot be read down a phone. Anything in FACTS is something the model
    may say, so the link must not be in there — only the address it went to."""
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    last = _turns(agent)[-1]
    assert BOOK_URL not in last["facts"]
    assert BOOK_URL not in last["directive"]
    assert "cli.transportpro.net" not in last["facts"] + last["directive"]
    # The address it went to IS there, and has to be said exactly.
    assert ON_ACCOUNT in last["facts"]


def test_only_the_agreed_rate_is_speakable(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")
    assert _turns(agent)[-1]["speakable"] == "$1600"


def test_the_note_records_the_link_and_that_it_is_unsigned(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    notes = _notes(repo)
    assert "Booking link issued" in notes
    assert "99001" in notes
    assert "NOT yet signed" in notes


# --------------------------------------------------------------------------- #
# Failure: the offer landed but no link came back
# --------------------------------------------------------------------------- #
def test_an_accept_failure_hands_over_and_says_the_offer_exists(fake, repo):
    """The dangerous case. The rate IS on the load, so the note has to say so —
    a rep who assumes otherwise creates a second offer against the same lane."""
    fake.json("/svc/happyrobot.php", {"response_code": 301, "message": "nope"})
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "transferred"
    assert agent.summary()["booking_link_sent"] is False

    notes = _notes(repo)
    assert "The offer IS recorded" in notes
    assert "99001" in notes
    assert "second one" in notes         # ...so don't create another
    # Nothing about being booked reached the carrier.
    assert "booked" not in _turns(agent)[-1]["directive"].lower()


def test_no_link_never_falls_back_to_logging_a_second_offer(fake, repo):
    """`POST /offer` may already have landed, so the log-only path must NOT run as
    a fallback — that is precisely how a lane gets double-sold."""
    fake.json("/svc/happyrobot.php", {"response_code": 301, "message": "nope"})
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "transferred"
    assert fake.calls("make_offer") == []


# --------------------------------------------------------------------------- #
# Failure: nothing was recorded at all
# --------------------------------------------------------------------------- #
def test_a_failed_offer_creation_says_nothing_was_recorded(fake, repo):
    fake.on("/offer", httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"))
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "transferred"
    notes = _notes(repo)
    assert "Nothing was recorded against the load" in notes
    assert "The offer IS recorded" not in notes
    # Never tried to accept an offer that doesn't exist.
    assert "accept_offer" not in _hr_actions(fake)


def test_an_unverified_address_still_books_nothing(fake, repo):
    """The email gate sits in front of all of this: an address not on the account
    means no offer, no link, no booking — whichever booking path is configured."""
    agent = _to_email(fake, repo)
    agent.handle("send it to dispatch at totally-different-domain dot com")

    assert agent.summary()["outcome"] != "booked"
    assert fake.calls("/offer") == []
    assert "accept_offer" not in _hr_actions(fake)


# --------------------------------------------------------------------------- #
# Without the credentials, the old path is untouched
# --------------------------------------------------------------------------- #
def test_no_happyrobot_credentials_keeps_the_logged_offer_path(fake, repo):
    """A deployment that hasn't configured the endpoint behaves exactly as before:
    the rate is logged via make_offer and the carrier is told they're booked."""
    fake.json(f"/voiceai/load/{LOAD}/make_offer", {"offer_id": 42})
    agent = _to_email(fake, repo, happyrobot_url="", happyrobot_token="")
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "booked"
    assert agent.summary()["booking_link_sent"] is False
    assert fake.bodies("make_offer")[0]["offer_amount"] == "1600"
    # No offer/accept round trip happened, and with no credentials the HappyRobot
    # endpoint was never reachable at all.
    assert fake.calls("/offer") == []
    assert _hr_bodies(fake) == []
    # And this path still says "booked", which is what it has always said.
    assert "booked" in _turns(agent)[-1]["directive"].lower()
