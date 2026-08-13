"""
Whole calls, driven end to end against the fake Transport Pro.

This is the file that pins the desk requirement in order:

  1. the load number is verified against the board
  2. the MC has to be in the system as ACTIVE — INACTIVE or SUSPENDED is told
     their company doesn't meet the requirements to work with us
  3. only then: where and when is the truck empty
  4. then the load details, its board notes, and the floor rate
  5. negotiate from there
  6. on agreement, the email has to already be on their account before anyone
     hears "booked" — and the booking has to land in Transport Pro first

The composer is a stub, so these assert on what the state machine DECIDED and
what it authorised the model to say, never on prose.
"""

import httpx
import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.voice import StubComposer
from tests.transportpro_fake import FakeTransportPro, board, repository, settings
from tests.transportpro_payloads import (
    CARRIER_STATUS_ACTIVE,
    CARRIER_STATUS_INACTIVE,
    CARRIER_STATUS_LIVE_ACTIVE,
    CARRIER_STATUS_LIVE_FAIL,
    CARRIER_STATUS_LIVE_REVIEW,
    CARRIER_STATUS_NO_STATUS_FIELD,
    CONTACT_SEARCH,
    EMPTY_SEARCH,
    LOAD_DETAIL_BOOKABLE,
    LOAD_DETAIL_UNPOSTED,
    record_for,
)

LOAD = "1303369"                                  # seven digits, as Transport Pro keys them
ON_ACCOUNT = "dispatch@blueskylogistics.com"      # in CONTACT_SEARCH
EMPTY = "empty in Nashville, Tennessee today"


@pytest.fixture
def fake():
    """A board with load 1303369 posted at $1600 / max buy $1800, and Blue Sky
    Logistics active with two addresses on their account."""
    server = FakeTransportPro()
    board(server, record_for(int(LOAD)))
    server.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    server.json("/contact/search", CONTACT_SEARCH)
    server.json(f"/voiceai/load/{LOAD}/make_offer", {"offer_id": 42})
    server.json(f"/voiceai/load/{LOAD}/add_note", {})
    return server


def _agent(fake, repo, **overrides):
    config = settings(max_negotiation_rounds=6, **overrides)
    return CarrierSalesAgent(repository(fake, repo, **overrides),
                             StubComposer(), settings=config)


def _turns(agent):
    return agent._composer.turns


def _notes(repo):
    conn = repo._db.connect()
    try:
        return " ".join(r["note"] for r in
                        conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()


def _to_rate(fake, repo, load=LOAD, mc="MC 123456"):
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"calling about load {load}")
    agent.handle(mc)
    agent.handle(EMPTY)
    # The pitch and the requirements are separate turns now — read together
    # they ran to 25 seconds of speech and hit the token limit mid-sentence.
    agent.handle("go ahead")                # -> the requirements get read
    agent.handle("yeah we can do that")     # -> and confirmed
    return agent


# --------------------------------------------------------------------------- #
# 1. The load number
# --------------------------------------------------------------------------- #
def test_a_seven_digit_load_number_is_heard_and_verified(fake, repo):
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle("hey, calling on load 1303369")
    assert agent.load.load_id == LOAD
    assert agent.state.value == "verify_carrier"
    # Nothing about the freight has reached the model yet.
    facts = _turns(agent)[-1]["facts"]
    assert "Nashville" not in facts and "Miami" not in facts
    assert _turns(agent)[-1]["speakable"] == ""


def test_digits_read_one_at_a_time_still_resolve(fake, repo):
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle("one three zero three three six nine")
    assert agent.load.load_id == LOAD


def test_a_load_not_on_the_board_is_not_pitched(fake, repo):
    agent = _agent(fake, repo)          # nothing routed for 9999999 -> 404
    agent.greeting()
    agent.handle("load 9999999")
    assert agent.load is None
    assert agent.state.value == "identify_load"


def test_a_load_not_ready_to_dispatch_is_not_pitched_and_not_called_covered(
        fake, repo):
    """The desk sells Ready To Dispatch only. A load that is on the system but not
    released must not be sold — and must not be described as "already covered",
    which is a specific claim about the freight that a caller will repeat."""
    board(fake, record_for(int(LOAD),
                                 load_status="Available"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")

    assert agent.state.value == "identify_load"      # never advanced
    directive = _turns(agent)[-1]["directive"].lower()
    assert "not released for booking" in directive
    assert "isn't available to book" in directive
    assert "do not say it's covered" in directive
    assert _turns(agent)[-1]["speakable"] == ""


def test_a_load_with_posting_switched_off_is_not_pitched(fake, repo):
    board(fake, record_for(int(LOAD),
                                 postingInfo={"isPosted": False}))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")

    assert agent.state.value == "identify_load"
    assert "isn't posted" in _turns(agent)[-1]["directive"].lower()
    assert _turns(agent)[-1]["speakable"] == ""


def test_a_genuinely_covered_load_is_still_called_covered(fake, repo):
    """The specific claim is still made when it is actually true."""
    board(fake, record_for(int(LOAD),
                                 load_status="Covered"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    assert "already covered" in _turns(agent)[-1]["directive"].lower()


def test_a_covered_load_ends_the_call_without_a_cross_sell(fake, repo):
    """The desk's instruction: tell them, thank them, done.

    A carrier ringing about one specific posting is not shopping a list, and
    reading five other load numbers at somebody who wanted that lane is the part
    that sounds like a machine. So this branch closes the call rather than pivoting
    into a pitch.
    """
    board(fake, record_for(int(LOAD), load_status="Covered"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")

    assert agent.state.value == "done"
    assert agent.summary()["outcome"] == "no_deal"

    directive = _turns(agent)[-1]["directive"].lower()
    assert "already covered" in directive
    assert "thank them for calling" in directive
    # The prohibitions have to be explicit, since every other unsellable branch
    # DOES offer alternatives and the model has seen those patterns.
    assert "do not offer another load" in directive
    assert "do not read out any other load number" in directive


def test_a_covered_load_never_puts_another_load_number_in_reach(fake, repo):
    """Belt and braces on the guardrail rather than the prompt: any figure not in
    the directive or facts is a breach, so the open-load numbers being absent from
    BOTH is what actually makes them unspeakable."""
    board(fake, record_for(int(LOAD), load_status="Covered"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")

    turn = _turns(agent)[-1]
    source = turn["directive"] + turn["facts"]
    # The load they asked about is sayable; nothing else numeric is.
    assert LOAD in source
    assert turn["speakable"] == ""
    for other in ("1303370", "1303371", "L1002", "L1003"):
        assert other not in source, other


def test_a_covered_load_does_not_scan_the_board_at_all(fake, repo):
    """Latency, not just wording. The open-load scan is a round trip per office
    terminal and the caller waits through it — so a branch that offers nothing must
    not pay for the list it will never read."""
    board(fake, record_for(int(LOAD), load_status="Covered"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")

    assert agent.state.value == "done"
    assert fake.calls("/load/search") == []
    # The load itself WAS fetched — that is how we know it is covered.
    assert fake.calls(f"/load/{LOAD}")


def test_no_branch_scans_the_board_any_more(fake, repo):
    """The agent never reads out a list, so it never needs one. The board scan is
    a round trip per office terminal, and it is now off the call path entirely."""
    for kwargs in ({"load_status": "Covered"},
                   {"postingInfo": {"isPosted": False}},
                   {"load_status": "Available"}):
        server = FakeTransportPro()
        board(server, record_for(int(LOAD), **kwargs))
        agent = _agent(server, repo)
        agent.greeting()
        agent.handle(f"load {LOAD}")
        assert server.calls("/load/search") == [], kwargs


def test_a_covered_load_records_which_load_it_was(fake, repo):
    """The call ends before `self.load` would normally be set, so it is set anyway —
    a call record with no load id is one nobody can reconcile against the board."""
    board(fake, record_for(int(LOAD), load_status="Covered"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")

    assert agent.summary()["load_id"] == LOAD
    assert "is covered" in _notes(repo)
    assert "without offering alternatives" in _notes(repo)


def test_no_unsellable_state_ever_offers_another_load(fake, repo):
    """The rule, across every branch: tell them about the load they asked for and
    nothing else. Five load numbers spoken down a phone at somebody who wanted one
    specific lane is what makes this sound like a machine."""
    for status, kwargs in (("not found", {}),
                           ("not posted", {"postingInfo": {"isPosted": False}}),
                           ("not ready", {"load_status": "Available"}),
                           ("covered", {"load_status": "Covered"})):
        server = FakeTransportPro()
        if status == "not found":
            server.json(f"/load/{LOAD}", EMPTY_SEARCH)
            server.json("/load/search", EMPTY_SEARCH)
        else:
            board(server, record_for(int(LOAD), **kwargs))
        agent = _agent(server, repo)
        agent.greeting()
        agent.handle(f"load {LOAD}")

        turn = _turns(agent)[-1]
        source = turn["directive"] + turn["facts"]
        assert "do not offer" in source.lower(), status
        assert turn["speakable"] == "", status
        # Nothing numeric beyond the load they actually asked about.
        for other in ("1303370", "1303371", "L1002", "L1003"):
            assert other not in source, (status, other)


def test_the_three_non_covered_states_keep_the_call_open(fake, repo):
    """Nobody else has that freight, so the caller is asked whether they have
    another number — a real rep's next line, and the caller may well be holding a
    second posting. Only COVERED ends the call outright."""
    for status, kwargs in (("not posted", {"postingInfo": {"isPosted": False}}),
                           ("not ready", {"load_status": "Available"})):
        server = FakeTransportPro()
        board(server, record_for(int(LOAD), **kwargs))
        agent = _agent(server, repo)
        agent.greeting()
        agent.handle(f"load {LOAD}")

        assert agent.state.value == "identify_load", status
        assert agent.outcome is None, status
        assert "another number off the board" in _turns(agent)[-1]["directive"], status


def test_the_call_is_wrapped_up_after_three_numbers_that_do_not_work(fake, repo):
    """Removing the list removed the call's way forward, so this is the new one. A
    caller working from a stale posting, or a mangled transcript, must not be able
    to keep the line open indefinitely."""
    board(server := FakeTransportPro(), record_for(int(LOAD), load_status="Available"))
    agent = _agent(server, repo)
    agent.greeting()

    agent.handle(f"load {LOAD}")
    assert agent.state.value == "identify_load"
    agent.handle(f"load {LOAD}")
    assert agent.state.value == "identify_load"
    agent.handle(f"load {LOAD}")

    assert agent.state.value == "done"
    assert agent.summary()["outcome"] == "no_deal"
    directive = _turns(agent)[-1]["directive"].lower()
    assert "thank them for calling" in directive
    assert "do not ask for another number" in directive
    assert "3 load numbers in a row" in _notes(repo)


def test_no_rate_is_ever_authorised_for_a_load_that_is_not_sellable(fake, repo):
    for status in ("Available", "Covered", "Planned", "Cancelled"):
        board(fake, record_for(int(LOAD),
                                     load_status=status))
        agent = _agent(fake, repo)
        agent.greeting()
        agent.handle(f"load {LOAD}")
        agent.handle("MC 123456")
        assert all(t["speakable"] == "" for t in _turns(agent)), status
        assert fake.calls("make_offer") == [], status


# --------------------------------------------------------------------------- #
# The two real production loads, driven through a whole call
# --------------------------------------------------------------------------- #
def test_the_real_bookable_load_is_sold_at_its_posted_rate(repo):
    """Load 2520571: Ready To Dispatch, isPosted true, board rate 5025 / max buy
    5200. Every fact the carrier hears comes off the real payload."""
    server = FakeTransportPro()
    board(server, LOAD_DETAIL_BOOKABLE)
    server.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    server.json("/contact/search", CONTACT_SEARCH)
    server.json("/voiceai/load/2520571/make_offer", {"offer_id": 7})
    server.json("/voiceai/load/2520571/add_note", {})

    agent = _agent(server, repo)
    agent.greeting()
    agent.handle("calling on load 2520571")
    agent.handle("MC 123456")
    agent.handle("empty in Memphis, Tennessee today")

    # No board notes on this one (both stops carry only reference numbers), so it
    # goes straight to the rate.
    assert agent.state.value == "state_price"
    assert agent.neg.floor == 5025
    assert agent.neg.ceiling == 5200
    assert _turns(agent)[-1]["speakable"] == "$5025"

    facts = " ".join(t["facts"] for t in _turns(agent))
    assert "Sikeston, MO" in facts and "Vineland, NJ" in facts
    assert "Ice Cream" in facts
    assert "-20 F" in facts                 # the reefer setpoint is spoken
    assert "Reefer" in facts
    assert "38,309 lbs" in facts
    assert "#" not in facts                 # no separator junk from the notes
    assert "<br" not in facts

    agent.handle("that works")
    agent.handle("yep we can cover it")
    agent.handle(f"send it to {ON_ACCOUNT}")
    assert agent.summary()["outcome"] == "booked"
    assert server.bodies("make_offer")[0]["offer_amount"] == "5025"


def test_the_real_unposted_load_is_never_sold(repo):
    """Load 2520519: Ready To Dispatch but isPosted false. Same status as the one
    above, opposite outcome — and no rate is ever put in front of the carrier."""
    server = FakeTransportPro()
    board(server, LOAD_DETAIL_UNPOSTED)
    server.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)

    agent = _agent(server, repo)
    agent.greeting()
    agent.handle("calling on load 2520519")

    assert agent.state.value == "identify_load"        # never advanced
    assert "isn't posted" in _turns(agent)[-1]["directive"].lower()
    assert all(t["speakable"] == "" for t in _turns(agent))
    assert server.calls("make_offer") == []
    # And nothing about the freight leaked while refusing it.
    facts = " ".join(t["facts"] for t in _turns(agent))
    assert "Richmond" not in facts and "2890" not in facts


def test_the_shipper_instructions_gate_the_rate_on_a_real_load(repo):
    """With posting switched on, load 2520519's shipper note becomes the
    requirements gate: the driver check-in procedure is read and confirmed before
    any rate is discussed."""
    import copy

    record = copy.deepcopy(LOAD_DETAIL_UNPOSTED)
    record["postingInfo"] = {"isPosted": True, "loadBoardRate": 2100,
                             "maxBuy": 2400, "comments": None}
    server = FakeTransportPro()
    board(server, record)
    server.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    server.json("/contact/search", CONTACT_SEARCH)
    server.json("/voiceai/load/2520519/add_note", {})

    agent = _agent(server, repo)
    agent.greeting()
    agent.handle("load 2520519")
    agent.handle("MC 123456")
    agent.handle("empty in Richmond, Virginia today")

    assert agent.state.value == "check_requirements"
    reveal = _turns(agent)[-1]
    assert reveal["speakable"] == ""                    # still no rate
    assert "CARRIER MUST SEND BOL" in reveal["facts"]
    assert "trailer license plate number" in reveal["facts"]
    assert "<br" not in reveal["facts"]

    agent.handle("no, we can't do the plate paperwork")
    assert agent.summary()["outcome"] == "no_deal"
    assert all(t["speakable"] == "" for t in _turns(agent))


def test_the_board_going_down_hands_the_call_over_rather_than_denying_the_load(
        fake, repo):
    fake.on(f"/load/{LOAD}",
            httpx.Response(500, text="boom"), httpx.Response(500, text="boom"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle("load 1303369")
    assert agent.summary()["outcome"] == "transferred"
    assert "System of record unavailable" in _notes(repo)


# --------------------------------------------------------------------------- #
# 2. The MC has to be ACTIVE
# --------------------------------------------------------------------------- #
def test_an_active_mc_moves_on_to_the_empty_call(fake, repo):
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 123456")
    assert agent.carrier.legal_name == "Blue Sky Logistics LLC"
    assert agent.state.value == "ask_empty"
    directive = _turns(agent)[-1]["directive"].lower()
    assert "empty" in directive
    # Still no lane and still no money.
    assert "nashville" not in _turns(agent)[-1]["facts"].lower()
    assert _turns(agent)[-1]["speakable"] == ""


def test_an_inactive_mc_is_told_it_does_not_meet_the_requirements(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_INACTIVE)
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 555444")

    assert agent.summary()["outcome"] == "rejected"
    assert agent.state.value == "done"
    directive = _turns(agent)[-1]["directive"].lower()
    assert "does not currently meet the requirements" in directive
    # And it must not say WHY, or name a rate, or name the lane.
    assert "do not say which check failed" in directive
    assert "do not mention authority" in directive
    assert _turns(agent)[-1]["speakable"] == ""
    assert "nashville" not in _turns(agent)[-1]["facts"].lower()
    assert "not ACTIVE" in _notes(repo)


def test_a_suspended_mc_is_treated_the_same_way(fake, repo):
    fake.json("/voiceai/carrier_status",
              dict(CARRIER_STATUS_ACTIVE, carrier_status="suspended"))
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 123456")
    assert agent.summary()["outcome"] == "rejected"
    assert "does not currently meet the requirements" in \
        _turns(agent)[-1]["directive"].lower()


def test_the_live_fail_verdict_is_told_it_does_not_meet_the_requirements(fake, repo):
    """`FAIL` is what the live tenant returns for a carrier who did not pass
    vetting — the vocabulary is nothing like "inactive", and it still has to
    produce the desk's sentence."""
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_LIVE_FAIL)
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 23152")

    assert agent.summary()["outcome"] == "rejected"
    directive = _turns(agent)[-1]["directive"].lower()
    assert "does not currently meet the requirements" in directive
    assert all(t["speakable"] == "" for t in _turns(agent))
    assert "'FAIL'" in _notes(repo)          # the source's own word, in the log


def test_the_live_review_verdict_goes_to_onboarding_not_to_a_refusal(fake, repo):
    """`REVIEW` means onboarding is unfinished. The carrier has failed nothing, so
    they must NOT hear that they don't meet our requirements — a rep picks it up,
    and onboarding can often finish it on the call."""
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_LIVE_REVIEW)
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 277164")

    assert agent.summary()["outcome"] == "transferred"
    directive = _turns(agent)[-1]["directive"].lower()
    assert "does not currently meet the requirements" not in directive
    assert "rep" in directive
    # Still no load and no rate — pending is not a yes either.
    assert all(t["speakable"] == "" for t in _turns(agent))
    assert "nashville" not in " ".join(t["facts"] for t in _turns(agent)).lower()
    assert "authority_pending_review" in _notes(repo)


def test_the_live_active_verdict_proceeds(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_LIVE_ACTIVE)
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 556949")
    assert agent.state.value == "ask_empty"
    assert agent.carrier.authority_status.can_haul


def test_an_unreadable_status_goes_to_a_person_not_to_a_refusal(fake, repo):
    """A status field our mapper can't find is our gap, not the carrier's fault.
    Telling a legitimate carrier they fail our requirements over it is a claim we
    can't back up, so a rep looks at it instead."""
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_NO_STATUS_FIELD)
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 313131")

    assert agent.summary()["outcome"] == "transferred"
    directive = _turns(agent)[-1]["directive"].lower()
    assert "does not currently meet the requirements" not in directive
    assert "authority_not_reported" in _notes(repo)


def test_no_rate_is_ever_authorised_for_a_carrier_who_does_not_clear(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_INACTIVE)
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 555444")
    assert all(turn["speakable"] == "" for turn in _turns(agent))
    assert fake.calls("make_offer") == []


# --------------------------------------------------------------------------- #
# 3 & 4. The empty call, then the load, its notes and the floor rate
# --------------------------------------------------------------------------- #
def test_the_empty_call_comes_before_the_load_details(fake, repo):
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 123456")
    agent.handle(EMPTY)

    assert agent._empty_location == "Nashville, Tennessee"
    assert agent._empty_when == "today"
    # The load's board notes are read at this point, and the rate still is not.
    directive = _turns(agent)[-1]["directive"].lower()
    assert "special requirements" in directive
    assert "nothing about rate yet" in directive
    assert _turns(agent)[-1]["speakable"] == ""
    assert agent.state.value == "check_requirements"


def test_the_load_notes_and_the_floor_rate_reach_the_carrier(fake, repo):
    agent = _to_rate(fake, repo)
    assert agent.state.value == "state_price"

    # The floor rate is the board's load_board_rate, and it is the ONLY figure
    # this turn may speak.
    assert agent.neg.floor == 1600
    assert agent.neg.ceiling == 1800
    assert _turns(agent)[-1]["speakable"] == "$1600"

    # The board note was spoken, on the turn that revealed the load.
    all_facts = " ".join(t["facts"] for t in _turns(agent))
    assert "test board comment" in all_facts
    assert "Nashville, TN" in all_facts and "Miami, FL" in all_facts
    assert "1,142" in all_facts                 # miles
    assert "Aircraft Metal" in all_facts        # commodity
    assert "5:22 PM to 6 PM AST" in all_facts   # pickup window, as wall clock


def test_a_carrier_who_cannot_meet_the_notes_never_hears_a_rate(fake, repo):
    agent = _agent(fake, repo)
    agent.greeting()
    agent.handle(f"load {LOAD}")
    agent.handle("MC 123456")
    agent.handle(EMPTY)
    agent.handle("no, we can't do that")
    assert agent.summary()["outcome"] == "no_deal"
    assert all(turn["speakable"] == "" for turn in _turns(agent))


# --------------------------------------------------------------------------- #
# 5. Negotiation, from the floor and never above max buy
# --------------------------------------------------------------------------- #
def test_negotiation_runs_between_the_board_rate_and_max_buy(fake, repo):
    agent = _to_rate(fake, repo)
    agent.handle("I need 2400")            # above max buy
    agent.handle("2400, that's my best")
    agent.handle("still 2400")
    # Whatever happens, nothing above $1800 was ever put on the table.
    spoken = [t["speakable"] for t in _turns(agent) if t["speakable"]]
    figures = [int(part.strip().lstrip("$"))
               for turn in spoken for part in turn.split(",")
               if part.strip().lstrip("$").isdigit()]
    assert figures and max(figures) <= 2400      # 2400 is THEIR number, quotable
    assert all(f <= 1800 or f == 2400 for f in figures)


def test_agreeing_at_the_floor_moves_to_the_operational_close(fake, repo):
    agent = _to_rate(fake, repo)
    agent.handle("yeah that works")
    assert agent.state.value == "confirm_booking"
    assert agent._agreed_rate == 1600


# --------------------------------------------------------------------------- #
# 6. The booking gate
# --------------------------------------------------------------------------- #
def _to_email(fake, repo):
    agent = _to_rate(fake, repo)
    agent.handle("yeah that works")
    agent.handle("yep, we can cover that pickup")
    assert agent.state.value == "confirm_email"
    return agent


def test_an_address_on_the_account_books_and_posts_the_offer(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "booked"
    assert agent._booking_email == ON_ACCOUNT

    # The offer landed in Transport Pro, with the agreed rate and that address.
    body = fake.bodies("make_offer")[0]
    assert body["offer_amount"] == "1600"
    assert body["email"] == ON_ACCOUNT
    assert body["carrier_name"] == "Blue Sky Logistics LLC"
    assert body["mc_number"] == "123456"

    # And only then is the carrier told, with the link going to that address.
    last = _turns(agent)[-1]
    assert ON_ACCOUNT in last["facts"]
    assert "booking" in last["directive"].lower()


def test_a_spoken_address_is_matched_against_the_account(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle("dispatch at blue sky logistics dot com")
    assert agent.summary()["outcome"] == "booked"
    assert agent._booking_email == ON_ACCOUNT


def test_an_address_not_on_the_account_books_nothing(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle("send it to dispatch at totally-different-domain dot com")

    assert agent.summary()["outcome"] is None       # nobody has been told anything
    assert agent.state.value == "confirm_email"
    assert fake.calls("make_offer") == []
    directive = _turns(agent)[-1]["directive"].lower()
    assert "not the one on their account" in directive
    assert "do not say they're booked yet" in directive


def test_an_address_not_on_the_account_twice_goes_to_a_rep(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle("dispatch at totally-different-domain dot com")
    agent.handle("no, dispatch at totally-different-domain dot com")

    assert agent.summary()["outcome"] == "transferred"
    assert fake.calls("make_offer") == []
    notes = _notes(repo)
    assert "NOT BOOKED" in notes and "not on their account" in notes
    assert "1600" in notes           # the agreed rate is preserved for the rep


def test_pointing_at_the_account_uses_the_address_on_it(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle("just use the one you've got on file")
    assert agent.summary()["outcome"] == "booked"
    assert agent._booking_email in (
        "dispatch@blueskylogistics.com", "billing@blueskylogistics.com")


def test_a_carrier_with_no_addresses_on_the_account_cannot_be_booked(fake, repo):
    fake.json("/contact/search", EMPTY_SEARCH)
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")
    agent.handle(f"really, {ON_ACCOUNT}")
    assert agent.summary()["outcome"] == "transferred"
    assert fake.calls("make_offer") == []


def test_a_booking_transport_pro_refuses_is_never_announced(fake, repo):
    """The failure this ordering exists for: if `make_offer` doesn't land, there is
    no load against their name, and a carrier who was told otherwise turns up at a
    shipper for freight that isn't theirs."""
    fake.on(f"/voiceai/load/{LOAD}/make_offer",
            httpx.Response(500, text="boom"), httpx.Response(500, text="boom"))
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")

    assert agent.summary()["outcome"] == "transferred"
    assert "Could NOT record" in _notes(repo)
    assert "booked" not in _turns(agent)[-1]["directive"].lower()


def test_call_notes_are_mirrored_onto_the_load_in_transport_pro(fake, repo):
    agent = _to_email(fake, repo)
    agent.handle(f"send it to {ON_ACCOUNT}")
    posted = " ".join(body["content"] for body in fake.bodies("add_note"))
    assert "Voice AI call" in posted
    assert "Booking 1303369" in posted
