"""
A carrier `/voiceai/carrier_status` has never heard of — and the decline it earns.

The live call this comes from: a caller gave MC 1798414. That MC is ABSENT from
`/voiceai/carrier_status`, so `get_carrier` returned None, which reads as
"not_found" — the agent's re-ask path. It asked twice ("I've got one seven nine
eight four one four — what comes after that?"), ran out of attempts, and handed the
call to a rep with "Perfect, I've got you verified."

Three things were wrong, and all three are pinned here:

  * the carrier IS on file, on the HappyRobot endpoint, as an explicit `FAIL`
  * Highway had it too — every classification failing, `overall_result: "fail"`,
    `needs_to_connect_eld` — and nothing read that verdict
  * "I've got you verified" was said about the one thing the call had failed to
    establish

The right answer is a DECLINE: their company does not currently meet the
requirements to work with us. Not a re-ask, and certainly not a congratulation.
"""

import httpx
import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.domain.models import AuthorityStatus, VerificationAction
from lanevoice.services import CarrierVerificationService
from lanevoice.voice import StubComposer
from tests.transportpro_fake import (
    HAPPYROBOT_URL,
    FakeTransportPro,
    board,
    repository,
    settings,
)
from tests.transportpro_payloads import CONTACT_SEARCH, EMPTY_SEARCH, record_for

LOAD = "1303369"
MC = "1798414"

# The real HappyRobot row for MC 1798414. Note `id: null` and the numeric MC/DOT —
# both are how that endpoint actually answers.
HR_FAIL_ROW = {
    "action": "carrier_lookup",
    "data": [{
        "id": None,
        "status": "FAIL",
        "carrier_name": "DS35 ENTERPRISES",
        "city": "SAVANNAH",
        "state": "TX",
        "us_dot_number": 4534301,
        "mc_number": 1798414,
        "classifications": None,
    }],
}

_HR = {"happyrobot_url": HAPPYROBOT_URL, "happyrobot_token": "hr-token"}


@pytest.fixture
def fake():
    """A board with the load, and a carrier_status that knows nobody."""
    server = FakeTransportPro()
    board(server, record_for(int(LOAD)))
    server.json("/voiceai/carrier_status", EMPTY_SEARCH)   # the carrier is NOT here
    server.json("/contact/search", CONTACT_SEARCH)
    server.json("/svc/happyrobot.php", HR_FAIL_ROW)
    return server


def _repo(fake, audit, **overrides):
    return repository(fake, audit, **(_HR | overrides))


def _hr_actions(fake):
    import json
    return [json.loads(r.content)["action"] for r in fake.calls("happyrobot.php")]


# --------------------------------------------------------------------------- #
# The fallback lookup
# --------------------------------------------------------------------------- #
def test_a_carrier_missing_from_carrier_status_is_still_found(fake, repo):
    """The bug. `/voiceai/carrier_status` does not have every carrier the desk
    knows about, and treating its silence as "no such carrier" turned a definite
    FAIL into a re-ask."""
    carrier = _repo(fake, repo).get_carrier(MC)

    assert carrier is not None
    assert carrier.mc_number == "1798414"
    assert carrier.usdot_number == "4534301"
    assert carrier.raw_authority_status == "FAIL"
    assert carrier.authority_status is AuthorityStatus.SUSPENDED
    assert carrier.authority_status.can_haul is False
    # A definite answer, which is what makes this a decline and not a handoff.
    assert carrier.authority_status.is_definite is True


def test_carrier_status_wins_when_it_has_the_carrier(fake, repo):
    """It is a fallback, not a second opinion: the FAIL row on the HappyRobot
    endpoint must not override a carrier publicapi already answered for."""
    from tests.transportpro_payloads import CARRIER_STATUS_ACTIVE

    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    carrier = _repo(fake, repo).get_carrier("123456")

    assert carrier is not None
    assert carrier.authority_status is AuthorityStatus.ACTIVE
    assert carrier.raw_authority_status != "FAIL"


def test_the_lookup_is_not_requested_twice(fake, repo):
    """`carrier_lookup` is BOTH the fallback and the source of the classification
    list. When the fallback fires, its response has to be reused — the same request
    twice, on the critical path of a live call, for one carrier."""
    _repo(fake, repo).get_carrier(MC)
    assert _hr_actions(fake).count("carrier_lookup") == 1


def test_without_happyrobot_the_carrier_stays_not_found(fake, repo):
    """No credentials means no fallback, and the old behaviour: a re-ask, then a
    rep. A reduced capability, not an error."""
    tp = repository(fake, repo, happyrobot_url="", happyrobot_token="")
    assert tp.get_carrier(MC) is None


def test_a_failing_fallback_lookup_does_not_break_the_call(fake, repo):
    """A carrier we cannot look up at all must stay "not found" — the re-ask path —
    rather than raising in the middle of a conversation."""
    fake.on("/svc/happyrobot.php", httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"))
    assert _repo(fake, repo).get_carrier(MC) is None


def test_the_fallback_tries_dot_as_well_as_mc(fake, repo):
    """A caller rarely labels which number they said."""
    import json

    def only_dot(request):
        body = json.loads(request.content)
        if "dot_number" in body.get("data", {}):
            return httpx.Response(200, json=HR_FAIL_ROW)
        return httpx.Response(200, json={"response_code": 300, "message": "none",
                                         "data": None})

    fake.on("/svc/happyrobot.php", only_dot)
    assert _repo(fake, repo).get_carrier(MC) is not None


# --------------------------------------------------------------------------- #
# ...and the verdict it produces
# --------------------------------------------------------------------------- #
def test_the_carrier_is_declined_not_handed_over(fake, repo):
    """The whole point. A FAIL is a definite no, so the caller is told their
    company doesn't meet the requirements — not put on hold for a rep."""
    tp = _repo(fake, repo)
    result = CarrierVerificationService(tp).verify(MC, tp.get_load(LOAD))

    assert result.action == VerificationAction.DECLINE
    assert result.reason == "authority_not_active"
    assert result.verified is False
    assert result.invite_to_onboard is False


def test_the_whole_call_ends_in_a_rejection(fake, repo):
    """End to end, through the state machine: the caller hears the requirements
    line and the call is over. Nothing about the lane, nothing about a rate."""
    agent = CarrierSalesAgent(_repo(fake, repo), StubComposer(),
                              settings=settings(**_HR))
    agent.greeting()
    agent.handle(f"calling about load {LOAD}")
    agent.handle(f"my MC is {MC}")
    agent.handle("yes, that's us")              # the name read back is confirmed first

    assert agent.summary()["outcome"] == "rejected"
    directive = agent._composer.turns[-1]["directive"].lower()
    assert "does not currently meet the requirements" in directive
    # It is told NOT to name the failing check — the prohibitions have to be
    # present, which is the opposite of a substring search for their absence.
    assert "do not say which check failed" in directive
    assert "do not mention authority" in directive
    # And no money is speakable on a rejection.
    assert agent._composer.turns[-1]["speakable"] == ""


def test_the_rejection_is_recorded_with_the_real_reason(fake, repo):
    """The caller hears a vague line; the audit trail has to be specific."""
    agent = CarrierSalesAgent(_repo(fake, repo), StubComposer(),
                              settings=settings(**_HR))
    agent.greeting()
    agent.handle(f"calling about load {LOAD}")
    agent.handle(f"my MC is {MC}")
    agent.handle("yes, that's us")

    conn = repo._db.connect()
    try:
        notes = " ".join(r["note"] for r in
                         conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()
    assert "FAIL" in notes
    assert "1798414" in notes or "4534301" in notes


# --------------------------------------------------------------------------- #
# The handoff must not claim what the call failed to establish
#
# From the same live call: handed over because the MC could not be captured at
# all, the agent opened with "Perfect, I've got you verified." That is a flat
# untruth about the one thing the call had just failed to do — and it is the sort a
# carrier repeats to the rep who picks up.
# --------------------------------------------------------------------------- #
def _to_handoff(fake, repo):
    """Drive a call to a rep handoff via an MC that cannot be captured at all."""
    fake.on("/svc/happyrobot.php",
            httpx.Response(200, json={"response_code": 300, "message": "none",
                                      "data": None}))
    agent = CarrierSalesAgent(_repo(fake, repo), StubComposer(),
                              settings=settings(**_HR))
    agent.greeting()
    agent.handle(f"calling about load {LOAD}")
    for _ in range(4):
        agent.handle("uh it's like nine nine")     # too short to be a number
        if agent.state.value == "done":
            break
    return agent


def test_a_handoff_is_forbidden_from_claiming_verification(fake, repo):
    agent = _to_handoff(fake, repo)
    assert agent.summary()["outcome"] == "transferred"

    directive = agent._composer.turns[-1]["directive"]
    assert "Do NOT say or imply they are verified" in directive
    for word in ("approved", "cleared", "set up", "good to go"):
        assert word in directive, word
    assert "do not say 'perfect'" in directive.lower()


def test_the_handoff_names_no_money(fake, repo):
    agent = _to_handoff(fake, repo)
    assert agent._composer.turns[-1]["speakable"] == ""
