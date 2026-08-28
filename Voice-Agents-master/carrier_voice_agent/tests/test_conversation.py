"""End-to-end flow tests through the CarrierSalesAgent (no models, no keys).

The agent has no scripted replies — every line is composed by an LLM from a
directive plus facts. So these tests drive it with a fake composer and assert on
what the state machine DECIDED: which state it moved to, which outcome it
recorded, which dollar figures it authorised, and what it instructed the composer
to achieve. They deliberately do not assert on prose, because a real model words
each turn differently and a test that pins the wording would be testing the
mock.
"""

import re

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.conversation.agent import _MAX_MC_ASKS
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer

# The empty call now sits between verification and the load details, so every
# path to a rate goes through it.
EMPTY = "empty in Dallas, Texas today"

# An address that IS on Blue Sky Logistics' file in the seed data. The booking
# gate only confirms a booking for an address already on the carrier's account,
# so every test that expects to reach "booked" has to give one of theirs.
ON_FILE = "dispatch@blueskylogistics.com"


def _agent(repo, max_rounds=6, composer=None):
    settings = get_settings().model_copy(update={"max_negotiation_rounds": max_rounds})
    return CarrierSalesAgent(repo, composer or StubComposer(), settings=settings)


def _to_rate(repo, load="about L1001", mc="MC 123456", max_rounds=6, composer=None):
    """Drive a call to the point where a rate is on the table."""
    a = _agent(repo, max_rounds=max_rounds, composer=composer)
    a.greeting()
    a.handle(load)
    a.handle(mc)
    a.handle(EMPTY)
    return a


def _directives(agent) -> str:
    return " ".join(t["directive"] for t in agent._composer.turns).lower()


def _speakable(agent) -> list[str]:
    return [t["speakable"] for t in agent._composer.turns]


def _agent_offers(repo) -> set[int]:
    conn = repo._db.connect()
    try:
        return {int(r["amount"]) for r in conn.execute(
            "SELECT amount FROM negotiation_offers WHERE offered_by='agent'").fetchall()}
    finally:
        conn.close()


def _notes(repo) -> str:
    conn = repo._db.connect()
    try:
        return " ".join(r["note"] for r in
                        conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()


class _FixedComposer:
    """Says the same thing every turn, whatever it was told. Proves the guardrails
    catch a model that leaks a rate or drops our number."""

    def __init__(self, line: str):
        self._line = line
        self.turns: list[dict] = []

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        return self._line

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


class _RecoveringComposer:
    """Invents a rate on its first attempt at any turn that involves money, then
    complies once it's told what it did wrong — the retry path a real model
    actually exercises. Turns with no money in play are answered plainly so the
    call can reach the negotiation."""

    def __init__(self):
        self.turns: list[dict] = []

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        if not speakable:
            return "Alright."
        if correction:
            return f"Here's where I'm at: {speakable}."
        return "How about I meet you at $9999?"      # a figure nobody authorised

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


# --------------------------------------------------------------------------- #
# Authority: only ACTIVE clears the desk
# --------------------------------------------------------------------------- #
def test_active_authority_is_the_only_status_that_gets_a_rate(repo):
    a = _to_rate(repo, "L1001", "MC 123456")
    assert a.state.value == "state_price"
    assert a.carrier.authority_status.value == "active"


def test_suspended_authority_never_reaches_a_rate(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC999888")                       # Ghost Carrier — suspended
    assert a.summary()["outcome"] == "rejected"
    assert a.state.value == "done"
    assert _agent_offers(repo) == set()        # no number was ever authorised


def test_inactive_authority_never_reaches_a_rate(repo):
    """Lapsed rather than pulled — still a hard stop, because the company
    requirement is ACTIVE and nothing else."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 555444")                      # Dormant Transport — inactive
    assert a.summary()["outcome"] == "rejected"
    assert _agent_offers(repo) == set()


def test_a_blocked_carrier_is_told_nothing_about_why(repo):
    """The turn that declines them must not leak which check failed, and must not
    put the load in front of the composer at all."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC999888")
    last = a._composer.turns[-1]
    assert "do not say which check failed" in last["directive"].lower()
    assert "Chicago" not in last["facts"]      # the lane never got near the model
    assert last["speakable"] == ""             # and neither did any money


def test_not_approved_carrier_declined(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 222333")                      # active + insured, but not approved
    assert a.summary()["outcome"] == "rejected"


def test_risk_flagged_carrier_goes_to_a_human(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 777111")                      # recently reactivated
    assert a.summary()["outcome"] == "transferred"


# --------------------------------------------------------------------------- #
# Hearing an MC/USDOT number the way a person does
# --------------------------------------------------------------------------- #
def _to_mc(repo, load="L1002"):
    a = _agent(repo)
    a.greeting()
    a.handle(load)
    return a


def test_digits_are_remembered_across_turns(repo):
    """A caller cut off mid-number carries on from where they stopped. Their first
    attempt must not be thrown away — that's what forces the third re-ask."""
    a = _to_mc(repo)
    a.handle("six five four")
    assert a._mc_digits == "654"                 # held, not discarded
    assert a.state.value == "verify_carrier"
    a.handle("three two one")
    assert a._mc_digits == "654321"              # joined across the two turns
    assert a.carrier.legal_name == "Roadrunner Freight Inc"
    assert a.state.value == "ask_empty"


def test_a_caller_who_backs_up_and_repeats_is_not_double_counted(repo):
    """"six five four" ... "five four three two one" is 654321, not 654654321.
    The carrier file decides which reading was real."""
    a = _to_mc(repo)
    a.handle("6, 5, 4")
    a.handle("5, 4, 3, 2, 1")
    assert a._mc_digits == "654321"
    assert a.carrier is not None


def test_a_caller_who_starts_the_number_over_is_understood(repo):
    a = _to_mc(repo)
    a.handle("it's 654")
    a.handle("654321")
    assert a._mc_digits == "654321"
    assert a.carrier is not None


def test_partial_number_is_confirmed_by_company_name(repo):
    """One digit lost off the end still narrows to a single carrier. Reading the
    NAME back is easier to confirm on a bad line than collecting digits."""
    a = _to_mc(repo)
    a.handle("MC 65432")                         # 654321 with the tail dropped
    assert a.carrier is None                     # not verified on a guess
    turn = a._composer.turns[-1]
    assert "Roadrunner Freight Inc" in turn["facts"]
    assert "do NOT read the MC or USDOT digits back" in turn["directive"].lower() \
        or "do not read the mc or usdot digits back" in turn["directive"].lower()
    a.handle("yes, that's us")                   # the confirmation resolves it
    assert a.carrier.legal_name == "Roadrunner Freight Inc"
    assert a._mc_digits == "654321"              # the real number, off their file


def test_denying_the_name_discards_the_misheard_digits(repo):
    """If the name is wrong the digits behind it were wrong too — keeping them
    would just narrow to the same wrong carrier again."""
    a = _to_mc(repo)
    a.handle("MC 65432")
    a.handle("no, different company")
    assert a._mc_digits == ""
    assert a._mc_narrowed is None
    a.handle("MC 123456")
    assert a.carrier.legal_name == "Blue Sky Logistics LLC"


def test_the_agent_never_asks_a_caller_to_slow_down(repo):
    """Nobody working a freight desk says "one digit at a time". Asking for it is
    what made the caller on the real call hang up."""
    a = _to_mc(repo)
    for turn in ("uh hang on", "six five four", "mumble"):
        a.handle(turn)
        if a.state.value != "verify_carrier":
            break
    # Strip the sentences that explicitly FORBID this wording; nothing should be
    # left that still tells the agent to ask for it.
    instructions = re.sub(r"do not[^.]*", "", _directives(a))
    for banned in ("slow down", "slowly", "one digit at a time"):
        assert banned not in instructions, f"agent was told to ask for: {banned}"


def test_an_unreadable_number_reaches_a_person_not_a_loop(repo):
    a = _to_mc(repo)
    for _ in range(_MAX_MC_ASKS + 1):
        a.handle("static on the line")
        if a.state.value == "done":
            break
    assert a.summary()["outcome"] == "transferred"
    assert "Could not capture an MC/USDOT number" in _notes(repo)


# --------------------------------------------------------------------------- #
# The empty call gates the load details
# --------------------------------------------------------------------------- #
def test_load_details_are_withheld_until_the_empty_call(repo):
    """Nothing about the lane may reach the composer — let alone the caller —
    before we know where their truck is."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    assert a.state.value == "ask_empty"
    assert not a._load_revealed
    for turn in a._composer.turns:
        blob = f"{turn['directive']} {turn['facts']}"
        assert "Chicago" not in blob and "Dallas" not in blob
        assert "Dry Van" not in blob
        assert turn["speakable"] == ""          # and no rate either


def test_an_mc_read_one_digit_at_a_time_is_accepted(repo):
    """The form we actually ask for has to work first time."""
    a = _agent(repo)
    a.greeting()
    a.handle("Looking for load L1002")
    a.handle("6, 5, 4, 3, 2, 1.")
    assert a.carrier is not None
    assert a.carrier.legal_name == "Roadrunner Freight Inc"
    assert a.state.value == "ask_empty"          # straight on to the empty call


def test_an_uncaptured_mc_goes_to_a_person_instead_of_asking_forever(repo):
    """A caller who can't be heard gets a human, not a fourth attempt. Three
    unanswered asks in a row is what made a real caller hang up."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1002")
    for _ in range(_MAX_MC_ASKS):
        a.handle("uh, hang on, my phone's cutting out")
    assert a.summary()["outcome"] == "transferred"
    assert "Could not capture an MC/USDOT number" in _notes(repo)


def test_the_empty_call_asks_for_both_halves(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    assert "getting empty" in _directives(a) or "empty" in _directives(a)


def test_a_location_only_answer_is_followed_up_for_the_timing(repo):
    """The reference call: they say where, so you ask when — you don't re-ask the
    whole question like a form."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("empty in Towson, Arizona")
    assert a.state.value == "ask_empty"          # still gathering
    assert a._empty_location == "Towson, Arizona"
    assert a._empty_when is None
    last = a._composer.turns[-1]["directive"].lower()
    assert "not when" in last or "when it's going to be empty" in last
    assert "do not ask where again" in last
    a.handle("empty right now, ready to go")
    assert a._empty_when == "right now"
    assert a.state.value == "state_price"        # both halves in -> load comes out
    assert a._load_revealed


def test_a_greeting_is_not_a_place(repo):
    """Observed live: dead air made the caller say "Hello." — and the agent
    answered "Alright, Hello, got it" with the truck now empty in a town called
    Hello. A greeting, a line-check ("you there?") or a bare "right" is never a
    location; the agent keeps asking instead."""
    from lanevoice import parsing
    for line in ("Hello.", "hello", "hey", "hi", "you there?", "anybody there",
                 "can you hear me", "right", "alright"):
        assert parsing.extract_empty_location(line) is None, \
            f"{line!r} was read as a location"
    # And through the agent: the first greeting turn stays in ask_empty.
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("Hello.")
    assert a._empty_location is None
    assert a.state.value == "ask_empty"


def test_a_timing_only_answer_is_followed_up_for_the_place(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("it's empty right now")
    assert a._empty_when == "right now"
    assert a.state.value == "ask_empty"
    assert "do not ask when again" in a._composer.turns[-1]["directive"].lower()


def test_the_empty_call_is_logged_against_the_call(repo):
    _to_rate(repo)
    notes = _notes(repo)
    assert "Empty call" in notes
    assert "Dallas, Texas" in notes and "today" in notes


def test_the_truck_becomes_standing_context_for_later_turns(repo):
    """Once we know where their truck is, every later turn can use it — that's how
    the agent stops asking a question it already has the answer to."""
    a = _to_rate(repo)
    a.handle("I need 2500")
    facts = " ".join(t["facts"] for t in a._composer.turns)
    assert "Dallas, Texas" in facts
    # And the discovery probe knows not to ask where they're coming out of.
    assert "do NOT ask where they're coming out of" in \
        a._composer.turns[-1]["directive"]


def test_the_load_rundown_reaches_the_composer_in_full(repo):
    """The reference pitch needs all of it: lane, dates, windows, commodity,
    pieces, equipment, miles. If a fact isn't in FACTS the model can't say it."""
    a = _to_rate(repo)
    facts = a._composer.turns[-1]["facts"]
    for expected in ("Chicago, IL", "Dallas, TX", "Dry Van", "packaged food goods",
                     "925", "42,000 lbs", "7 AM to 2 PM", "8 AM to 12 PM", "26"):
        assert expected in facts, f"missing from the pitch facts: {expected}"


def test_dates_reach_the_composer_spoken_not_as_iso(repo):
    a = _to_rate(repo)
    facts = a._composer.turns[-1]["facts"]
    assert "2026-08-03" not in facts          # never read digits-and-dashes aloud
    assert "Picks up:" in facts


# --------------------------------------------------------------------------- #
# Money: the engine owns every figure, the model owns only the words
# --------------------------------------------------------------------------- #
def test_composer_cannot_invent_a_rate(repo):
    """An unauthorised dollar figure is rejected. The model gets re-prompted with
    the breach named, and if it keeps at it the call goes to a person — the
    invented number never reaches the caller."""
    bad = _FixedComposer("Tell you what, meet me at $2200 and it's yours.")
    a = _to_rate(repo, composer=bad)
    reply = a.handle("I want 2500")           # engine says: hold at $2000
    assert "2200" not in reply
    assert a.outcome.value == "transferred"   # handed off rather than speaking it
    assert "Could not compose a compliant turn" in _notes(repo)


def test_composer_cannot_invent_a_bare_rate_either(repo):
    """A bare '2200' is a leak too — spoken aloud it still moves the negotiation."""
    bad = _FixedComposer("We're still at $2000 on L1001 — can you meet me at 2200?")
    a = _to_rate(repo, composer=bad)
    reply = a.handle("I want 2500")
    assert "2200" not in reply


def test_composer_cannot_drop_our_offer(repo):
    """A reply that never states our number reads as accepting theirs."""
    bad = _FixedComposer("You came down to $2400, I'll meet you there. We good?")
    a = _to_rate(repo, composer=bad)
    a.handle("I want 2500")
    reply = a.handle("2400")
    assert "meet you there" not in reply
    assert a.state.value != "confirm_booking"   # never slid into booking at $2400


def test_a_corrected_composer_gets_its_turn_spoken(repo):
    """The retry is the normal path, not an exception: name the breach, get a
    compliant line, carry on with the call."""
    fixed = _RecoveringComposer()
    a = _to_rate(repo, composer=fixed)
    reply = a.handle("I want 2500")
    assert "9999" not in reply                 # the invented figure never got out
    assert reply == "Here's where I'm at: $2000, $2500."
    assert a.outcome is None                   # call still live
    assert a.state.value == "negotiate"
    correction = fixed.turns[-1]["correction"]
    assert "named money you were not given" in correction
    assert "$2000" in correction               # told exactly what it may say


class _BrokenComposer:
    """A composer that cannot reach its model at all."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.attempts = 0

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.attempts += 1
        raise self._exc

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


def test_an_unreachable_model_is_not_retried_into_dead_air(repo):
    """A bad key fails identically every time. Retrying it just burns round trips
    of silence on a live call, so we stop at the first one and hand off."""
    broken = _BrokenComposer(RuntimeError("Error code: 401 - Invalid API Key"))
    a = _agent(repo, composer=broken)
    a.greeting()
    assert broken.attempts == 1                     # not 3
    assert a.outcome.value == "transferred"
    assert "Invalid API Key" in _notes(repo)        # the reason is on the record


def test_a_flaky_model_is_retried(repo):
    """A transient failure is worth another go — unlike an auth error, it might
    work this time."""
    broken = _BrokenComposer(TimeoutError("read timed out"))
    a = _agent(repo, composer=broken)
    a.greeting()
    assert broken.attempts == a._settings.llm_attempts
    assert "read timed out" in _notes(repo)


def test_load_facts_do_not_smuggle_a_rate_to_the_composer(repo):
    """`Load.facts()` must never carry open_rate or ceiling_rate — the engine is
    the only source of a number, and a ceiling in the prompt is a leaked max."""
    load = repo.get_load("L1001")
    facts = load.facts()
    for forbidden in ("2000", "2500", "1400"):
        assert forbidden not in facts


def test_turns_before_a_rate_authorise_no_money_at_all(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle(EMPTY)
    # Only the turn that actually quotes may name a figure.
    assert _speakable(a)[:-1] == [""] * (len(_speakable(a)) - 1)
    assert _speakable(a)[-1] == "$2000"


# --------------------------------------------------------------------------- #
# Negotiation behaviour
# --------------------------------------------------------------------------- #
def test_happy_path_books_at_opening(repo):
    a = _to_rate(repo)
    a.handle("yeah that works")
    assert a.state.value == "confirm_booking"
    a.handle("yep, I can cover it")
    assert a.state.value == "confirm_email"
    a.handle(f"send it to {ON_FILE}")
    assert a.summary()["outcome"] == "booked"
    assert repo.get_load("L1001").status.value == "covered"


def test_close_enough_gap_is_just_booked(repo):
    a = _to_rate(repo)
    a.handle("I need 2080")
    a.handle("2050")
    assert a.state.value == "confirm_booking"
    a.handle("yep can cover it")
    a.handle(ON_FILE)
    assert a.summary()["outcome"] == "booked"
    assert a._agreed_rate == 2050


def test_carrier_who_grinds_still_gets_covered(repo):
    a = _to_rate(repo)
    for ask in ("I need 2500", "2500", "2400", "2300", "2200", "2200"):
        a.handle(ask)
        if a.state.value != "negotiate":
            break
    assert a.state.value == "confirm_booking"
    assert a._agreed_rate == 2200
    a.handle("yes I can cover it")
    a.handle(ON_FILE)
    assert a.summary()["outcome"] == "booked"


def test_agent_makes_the_carrier_come_down_instead_of_laddering(repo):
    """A carrier who keeps moving gets asked to keep moving. Across a whole grind
    the agent puts at most two numbers on the table — its opening and the one move
    it makes to close."""
    a = _to_rate(repo, max_rounds=8)
    for ask in ("I need 2500", "2450", "2400", "2350"):
        a.handle(ask)
    directives = _directives(a)
    assert "how close they can get" in directives
    assert "not raising your offer" in directives
    offers = _agent_offers(repo)
    assert len(offers) <= 2
    assert min(offers) == 2000


def test_holds_its_number_until_the_carrier_moves(repo):
    a = _to_rate(repo)
    a.handle("I want 2500")
    a.handle("no, 2500")
    assert _agent_offers(repo) == {2000}       # never raised itself
    assert "not moving on this turn" in _directives(a)


def test_a_declared_best_number_is_closed_not_pushed_again(repo):
    a = _to_rate(repo)
    a.handle("I need 2400")
    a.handle("2300, that's my best")
    assert a.neg.pulls == 0
    assert 2150 in _agent_offers(repo)          # 2000 + half the remaining $300
    assert "$2150" in _speakable(a)[-1]


def test_the_same_pitch_is_never_repeated_at_the_caller(repo):
    """Non-price levers are spent one per turn — the composer is handed a
    different one each time, so it cannot repeat itself even if it wanted to."""
    a = _to_rate(repo, max_rounds=8)
    for ask in ("I need 2500", "2500", "2400", "2300", "2200"):
        a.handle(ask)
    used = [t["facts"] for t in a._composer.turns]
    for lever in ("first in line", "sit on their dock", "chasing us to get paid"):
        assert sum(lever in f for f in used) <= 1, f"repeated pitch: {lever}"


def test_firm_carrier_inside_max_buy_is_handed_to_a_rep(repo):
    a = _to_rate(repo)
    for _ in range(6):
        a.handle("2400, that's my number")
        if a.state.value == "done":
            break
    assert a.summary()["outcome"] == "transferred"
    assert "ESCALATION" in _notes(repo)


def test_high_ask_ends_no_deal_with_note(repo):
    a = _to_rate(repo, load="load L1003", mc="MC654321", max_rounds=4)
    for _ in range(5):
        a.handle("I need 1500")
        if a.state.value == "done":
            break
    assert a.summary()["outcome"] == "no_deal"
    assert "NO DEAL" in _notes(repo)


def test_fraud_low_is_transferred(repo):
    a = _to_rate(repo)
    a.handle("I'll haul it for 900")
    assert a.summary()["outcome"] == "transferred"


def test_carrier_asks_for_human(repo):
    a = _to_rate(repo)
    a.handle("can I talk to a rep")
    assert a.summary()["outcome"] == "transferred"


# --------------------------------------------------------------------------- #
# Load selection and requirements
# --------------------------------------------------------------------------- #
def test_unposted_load_is_not_offered(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1005")                          # seeded as is_posted=0
    assert a.state.value == "identify_load"
    assert "isn't posted" in a._composer.turns[-1]["directive"]


def test_load_requirements_are_confirmed_before_any_rate(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1002")                          # has special requirements
    a.handle("MC 123456")
    a.handle(EMPTY)
    assert a.state.value == "check_requirements"
    assert a._composer.turns[-1]["speakable"] == ""     # no rate yet
    assert "zero degrees" in a._composer.turns[-1]["facts"]
    # The requirements are their own turn: this one reads them out...
    a.handle("go ahead")
    assert a.state.value == "check_requirements"
    assert "cover this load's requirements from FACTS" in \
        a._composer.turns[-1]["directive"]
    # ...and only an actual yes moves to the rate.
    a.handle("yeah, I can do that")
    assert a.state.value == "state_price"
    assert a._composer.turns[-1]["speakable"] == "$1400"


def test_load_requirements_declined_no_deal(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1002")
    a.handle("MC 123456")
    a.handle(EMPTY)
    a.handle("no, I can't run it that cold")
    assert a.summary()["outcome"] == "no_deal"


class _ScopedRepo:
    """The seeded repo, but every load belongs to some other office — the shape a
    Fort Wayne deployment sees when a caller reads out Chicago's load number."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_load(self, load_id):
        from lanevoice.domain.errors import LoadOutOfScope
        raise LoadOutOfScope(f"load {load_id} belongs to Tinley Park Office, "
                             "outside this desk's scope")


def test_a_caller_who_declines_more_numbers_gets_a_warm_close(repo):
    """"No, that's okay" is an ANSWER. Observed live: told a load wasn't posted,
    the caller bowed out politely and the agent asked for another number — the
    very thing they had just declined — so they hung up and the call recorded
    as abandoned instead of a clean no-deal."""
    a = _agent(repo)
    a.greeting()
    a.handle("about L9999")                  # not on the board -> asks for another
    assert a.state.value != "done"
    a.handle("And that's okay.")
    assert a.state.value == "done"
    assert a.summary()["outcome"] == "no_deal"
    last = a._composer.turns[-1]["directive"].lower()
    assert "thank them for calling" in last
    assert "number" not in last              # it stopped asking
    assert "declined to continue" in _notes(repo)


def test_a_bare_acknowledgment_is_not_a_goodbye(repo):
    """"Okay" mid-thought must not hang up on a caller who is still talking —
    the closer has to be the whole turn."""
    a = _agent(repo)
    a.greeting()
    a.handle("about L9999")
    a.handle("okay hang on, let me find the posting")
    assert a.state.value != "done"           # still waiting on their number


def test_is_closing_turn_is_the_workers_filler_gate():
    """The worker uses this to skip the dead-air filler in front of a goodbye —
    "Alright, let me check that." spoken before "thanks for calling" was heard
    live and reads as nonsense. Closers gate it; working turns don't."""
    from lanevoice.conversation import is_closing_turn

    for closer in ("No.  Thank you.", "no, that's okay", "And that's okay.",
                   "nah I'm good", "that's all, thanks", "nope", "goodbye"):
        assert is_closing_turn(closer), closer
    for working in ("about load 2520571", "okay hang on, let me find the posting",
                    "yeah that works", "no, I can't run it that cold",
                    "it's two five five five nine four eight"):
        assert not is_closing_turn(working), working


def test_another_offices_load_ends_the_call_warmly_on_the_first_hit(repo):
    """Out-of-scope is decided, not misheard: no second number can change whose
    desk the freight is, so the call must NOT enter the try-another-number loop.
    Observed live before this existed — the caller was sent hunting through
    their posting for a number that could never have worked."""
    a = _agent(_ScopedRepo(repo))
    a.greeting()
    a.handle("about L1001")
    assert a.state.value == "done"
    assert a.summary()["outcome"] == "no_deal"
    d = _directives(a)
    assert "different circle desk" in d
    assert "thank them for reaching out" in d
    # It closed on the FIRST hit — never asked for another number...
    assert "another number" not in d
    # ...and the audit trail says why, since "no such load" about a load the
    # company plainly has is confusing to debug. (_ScopedRepo delegates
    # log_note to the real repo, so the note lands in its DB.)
    assert "another office" in _notes(repo).lower()


# --------------------------------------------------------------------------- #
# The operational close
# --------------------------------------------------------------------------- #
def _to_email_step(repo, load="about L1001", mc="MC 123456"):
    a = _to_rate(repo, load=load, mc=mc)
    a.handle("yeah that works")
    a.handle("yep, I can cover it")
    return a


def test_agent_asks_for_the_email_and_suggests_nothing(repo):
    a = _to_email_step(repo)
    assert a.state.value == "confirm_email"
    directive = a._composer.turns[-1]["directive"].lower()
    assert "email address" in directive
    assert "do not invent, guess or suggest" in directive
    assert "@" not in a._composer.turns[-1]["facts"]    # no address put in its mouth


def test_an_address_on_the_account_books_and_gets_the_link(repo):
    before = repo.carrier_emails("DOT1000001")
    assert len(before) > 1
    a = _to_email_step(repo)
    a.handle("send it to billing at blue sky logistics dot com")
    assert a._booking_email == "billing@blueskylogistics.com"
    assert a.summary()["outcome"] == "booked"
    assert repo.carrier_emails("DOT1000001") == before   # nothing was added
    last = a._composer.turns[-1]
    assert "billing@blueskylogistics.com" in last["facts"]
    assert "booking" in last["directive"].lower()


# --------------------------------------------------------------------------- #
# The booking gate: an address that isn't on the account does NOT get a booking.
#
# This is the guard against the obvious attack on a voice desk — somebody who has
# picked up a real MC number talking us into mailing the booking link to an
# address they control. It is also the ordinary case of a misheard domain, which
# is why the first miss is a re-ask rather than a handoff.
# --------------------------------------------------------------------------- #
def test_unknown_address_is_queried_once_and_books_nothing(repo):
    before = repo.carrier_emails("DOT1000001")
    a = _to_email_step(repo)
    a.handle("booking at blue sky freight dot com")

    assert a.summary()["outcome"] is None          # still on the call
    assert a.state.value == "confirm_email"
    assert a._booking_email is None
    assert repo.carrier_emails("DOT1000001") == before   # NOT appended any more

    directive = a._composer.turns[-1]["directive"].lower()
    assert "not the one on their account" in directive
    assert "do not say they're booked yet" in directive
    # It must not read the real addresses out — that would hand an impostor the
    # answer, and it isn't how a rep confirms an address either.
    assert "do not read out any address" in directive
    for known in before:
        assert known not in a._composer.turns[-1]["facts"]


def test_unknown_address_twice_goes_to_a_rep_not_a_booking(repo):
    a = _to_email_step(repo)
    a.handle("booking at blue sky freight dot com")
    a.handle("no it's definitely booking at blue sky freight dot com")

    assert a.summary()["outcome"] == "transferred"
    assert a._booking_email is None
    notes = _notes(repo)
    assert "NOT BOOKED" in notes
    assert "not on their account" in notes
    assert "2000" in notes            # the agreed rate is still recorded for a rep


def test_carrier_can_point_at_the_address_on_file(repo):
    a = _to_email_step(repo)
    a.handle("just use the one you have on file")
    assert a._booking_email in repo.carrier_emails("DOT1000001")
    assert a.summary()["outcome"] == "booked"


def test_no_usable_email_is_asked_once_then_handed_over(repo):
    """Previously this booked the load and left the confirmation to follow. It
    can't any more: with no verified address there is nothing to send a booking
    link to, so nobody gets told they're booked."""
    a = _to_email_step(repo)
    a.handle("uh, hang on a sec")
    assert "ask again for the best address" in a._composer.turns[-1]["directive"]
    a.handle("I'll have to dig it up")
    assert a.summary()["outcome"] == "transferred"
    assert a._booking_email is None
    assert "NOT BOOKED" in _notes(repo)
    assert "no usable address given" in _notes(repo)


def test_booking_is_recorded_before_the_carrier_is_told(repo, monkeypatch):
    """If the system of record won't take the booking, the carrier must not hear
    that they're booked — a truck sent to a dock for a load nobody has a record
    of is the worst outcome on this call."""
    monkeypatch.setattr(repo, "record_booking", lambda *a, **k: False)
    a = _to_email_step(repo)
    a.handle(f"send it to {ON_FILE}")
    assert a.summary()["outcome"] == "transferred"
    assert "Could NOT record" in _notes(repo)
    # Nothing in the closing turn may claim a booking.
    assert "booked" not in a._composer.turns[-1]["directive"].lower()


def test_cannot_cover_pickup_is_transferred(repo):
    a = _to_rate(repo)
    a.handle("yeah that works")
    a.handle("actually I can't make that pickup day")
    assert a.summary()["outcome"] == "transferred"
