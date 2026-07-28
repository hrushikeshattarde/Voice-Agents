"""End-to-end flow tests through the CarrierSalesAgent (no models, no keys).

The agent has no scripted replies — every line is composed by an LLM from a
directive plus facts. So these tests drive it with a fake composer and assert on
what the state machine DECIDED: which state it moved to, which outcome it
recorded, which dollar figures it authorised, and what it instructed the composer
to achieve. They deliberately do not assert on prose, because a real model words
each turn differently and a test that pins the wording would be testing the
mock.
"""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer

# The empty call now sits between verification and the load details, so every
# path to a rate goes through it.
EMPTY = "empty in Dallas, Texas today"


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
    a.handle("send it to dispatch@blue.com")
    assert a.summary()["outcome"] == "booked"
    assert repo.get_load("L1001").status.value == "covered"


def test_close_enough_gap_is_just_booked(repo):
    a = _to_rate(repo)
    a.handle("I need 2080")
    a.handle("2050")
    assert a.state.value == "confirm_booking"
    a.handle("yep can cover it")
    a.handle("bill@carrier.com")
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
    a.handle("ops@blue.com")
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


def test_email_the_carrier_gives_is_matched_against_their_file(repo):
    before = repo.carrier_emails("DOT1000001")
    assert len(before) > 1
    a = _to_email_step(repo)
    a.handle("send it to billing at blue sky logistics dot com")
    assert a._booking_email == "billing@blueskylogistics.com"
    assert not a._email_is_new
    assert repo.carrier_emails("DOT1000001") == before
    assert "billing@blueskylogistics.com" in a._composer.turns[-1]["facts"]


def test_new_email_is_appended_to_the_carrier_file(repo):
    before = repo.carrier_emails("DOT1000001")
    a = _to_email_step(repo)
    a.handle("booking at blue sky freight dot com")
    assert a._booking_email == "booking@blueskyfreight.com"
    assert a._email_is_new
    after = repo.carrier_emails("DOT1000001")
    assert set(before) < set(after)
    assert repo.get_carrier("123456").contact_emails == after


def test_carrier_can_point_at_the_address_on_file(repo):
    a = _to_email_step(repo)
    a.handle("just use the one you have on file")
    assert a._booking_email in repo.carrier_emails("DOT1000001")
    assert a.summary()["outcome"] == "booked"


def test_no_usable_email_is_asked_once_then_flagged(repo):
    a = _to_email_step(repo)
    a.handle("uh, hang on a sec")
    assert "ask again for the best address" in a._composer.turns[-1]["directive"]
    a.handle("I'll have to dig it up")
    assert a.summary()["outcome"] == "booked"
    assert a._booking_email is None
    assert "NOT CAPTURED" in _notes(repo)
    # And the closing turn must not pretend a confirmation is on its way.
    assert "do NOT say the confirmation is already on its way" in \
        a._composer.turns[-1]["directive"]


def test_cannot_cover_pickup_is_transferred(repo):
    a = _to_rate(repo)
    a.handle("yeah that works")
    a.handle("actually I can't make that pickup day")
    assert a.summary()["outcome"] == "transferred"
