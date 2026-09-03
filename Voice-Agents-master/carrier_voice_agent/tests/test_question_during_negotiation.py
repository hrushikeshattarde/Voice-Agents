"""
"What's it pay?" — a question mid-negotiation, with no number in it.

This is the commonest thing a carrier says after hearing a lane, and it used to
end the call. The rate is deliberately absent from FACTS — the negotiation engine
owns it, not the load — so the turn that handles "they said something with no
number in it" authorised NO money at all. The model was therefore asked to answer
the question and forbidden from saying the only thing that answers it. It said the
number anyway, was rejected on all three attempts, and the call was handed to a
rep. Seen on a live call against load 2535130:

    rejected composed turn in state state_price — You named money you were not
      given. The only money you may say this turn is: NO dollar amount at all.
    (x3) handing call ... to a rep

The fix lets the agent restate ITS OWN STANDING OFFER, and nothing else. Restating
a number we already said is not a concession; every other figure is still a
breach, and the engine's position is untouched. The tests below pin both halves —
the permission and its limits — because widening what money the agent may utter is
the one change in this codebase that can quietly cost real money.
"""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer

EMPTY = "empty in Dallas, Texas today"


class _EchoesTheRate:
    """Answers "what's it pay" the way a real model does — with the figure.

    Under the old behaviour every one of these was rejected. It returns the rate
    only when the turn was given one, so turns that authorise nothing still
    compose cleanly and the call can get as far as the negotiation.
    """

    def __init__(self):
        self.turns: list[dict] = []

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        if not speakable:
            return "Alright."
        return f"It's paying {speakable} on this one. What do you need?"

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


class _InventsARate:
    """Complies until switched on, then names a figure nobody authorised.

    The switch is what lets the call reach the negotiation at all: a composer that
    invents on every money turn never gets past its own opening offer, so the
    branch under test would never be exercised.
    """

    def __init__(self):
        self.turns: list[dict] = []
        self.invent = False

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        if self.invent:
            return "I can get you $9999 on this one."
        if not speakable:
            return "Alright."
        return f"I've got it at {speakable}."

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


def _agent(repo, composer=None):
    settings = get_settings().model_copy(update={"max_negotiation_rounds": 6})
    return CarrierSalesAgent(repo, composer or StubComposer(), settings=settings)


def _to_rate(repo, composer=None):
    """Drive a call to the point where our opening offer is on the table."""
    agent = _agent(repo, composer)
    agent.greeting()
    agent.handle("about L1001")
    agent.handle("MC 123456")
    agent.handle(EMPTY)
    assert agent.state.value == "state_price"
    return agent


# --------------------------------------------------------------------------- #
# The bug
# --------------------------------------------------------------------------- #
def test_asking_what_it_pays_does_not_end_the_call(repo):
    """The regression. Three rejected attempts used to hand this to a rep."""
    agent = _to_rate(repo, _EchoesTheRate())
    opening = int(agent.neg.current_offer)

    agent.handle("what's it pay")

    assert agent.outcome is None, "the call was handed over"
    assert agent.state.value != "done"
    assert str(opening) in agent.transcript[-1][1]


def test_the_standing_offer_is_what_the_turn_authorises(repo):
    agent = _to_rate(repo, _EchoesTheRate())
    opening = int(agent.neg.current_offer)

    agent.handle("what's it pay")

    assert agent._composer.turns[-1]["speakable"] == f"${opening}"


def test_the_directive_says_to_repeat_it_not_to_move_it(repo):
    agent = _to_rate(repo)
    opening = int(agent.neg.current_offer)
    agent.handle("what's it pay")

    directive = agent._composer.turns[-1]["directive"]
    assert f"${opening}" in directive
    assert "SAME number you already gave them" in directive
    assert "Do NOT move it" in directive


# --------------------------------------------------------------------------- #
# The limits — what the permission must NOT open up
# --------------------------------------------------------------------------- #
def test_no_other_figure_is_speakable(repo):
    """The permission is exactly one number wide. An invented rate is still a
    breach, and still costs the call rather than reaching a carrier."""
    composer = _InventsARate()
    agent = _to_rate(repo, composer)
    composer.invent = True

    agent.handle("what's it pay")

    assert agent.outcome is not None          # rejected every attempt -> handed over
    assert "9999" not in agent.transcript[-1][1]


def test_answering_a_question_does_not_move_our_number(repo):
    """A question is not a concession. The engine's position has to be identical
    on the other side of it."""
    agent = _to_rate(repo, _EchoesTheRate())
    before = agent.neg.current_offer
    rounds_before = agent.neg.round

    agent.handle("what's it pay")

    assert agent.neg.current_offer == before
    assert agent.neg.round == rounds_before
    assert agent.neg.concessions == 0


def test_the_rate_is_never_forced_into_an_unrelated_answer(repo):
    """`must_say` stays unset, so "how much does it weigh" gets an answer about
    weight. Volunteering the rate at every question would be a rep who can't stop
    negotiating with themselves."""
    agent = _to_rate(repo, _EchoesTheRate())
    agent.handle("how much does it weigh")

    turn = agent._composer.turns[-1]
    assert "leave the rate alone" in turn["directive"]
    # Authorised, but not compelled — a reply with no figure is accepted.
    assert agent.outcome is None


def test_a_question_does_not_log_a_carrier_offer(repo):
    """They named no number, so there is nothing to record as their ask. A logged
    phantom offer would distort the audit trail and the engine's history."""
    conn = repo._db.connect()
    try:
        agent = _to_rate(repo, _EchoesTheRate())
        agent.handle("what's it pay")
        rows = conn.execute(
            "SELECT COUNT(*) c FROM negotiation_offers WHERE offered_by='carrier'"
        ).fetchone()
        assert rows["c"] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The paths this branch must not swallow
# --------------------------------------------------------------------------- #
def test_a_real_counter_offer_still_negotiates(repo):
    """A number in the text goes to the engine as before — this branch is only for
    turns with no number in them."""
    agent = _to_rate(repo)
    agent.handle("I need $2500")
    assert agent.state.value in ("negotiate", "confirm_booking", "done")
    assert agent.neg.asks == [2500.0]


def test_a_plain_yes_still_books(repo):
    agent = _to_rate(repo)
    agent.handle("yeah that works")
    assert agent.state.value == "confirm_booking"


def test_asking_for_a_human_still_transfers(repo):
    """One of the phrases the person-request matcher knows — it is a fixed
    pattern, not intent detection, so the test uses a phrase that is on it."""
    agent = _to_rate(repo)
    agent.handle("can I speak to someone about this")
    assert agent.outcome is not None
    assert agent.summary()["outcome"] == "transferred"


def test_an_unrecognised_request_for_a_human_falls_into_the_question_branch(repo):
    """"Can I get somebody on the line" is NOT a phrase the person-request matcher
    knows, so it lands here instead — answered, with the standing offer speakable,
    rather than transferred. Worth pinning: it is the branch's job to handle
    everything the fixed patterns miss."""
    agent = _to_rate(repo, _EchoesTheRate())
    agent.handle("can I get somebody on the line")
    assert agent.outcome is None
    assert agent._composer.turns[-1]["speakable"] == f"${int(agent.neg.current_offer)}"


def test_a_bare_no_still_makes_our_move(repo):
    """"No" with no number after they've asked once is a hold, not a question."""
    agent = _to_rate(repo)
    agent.handle("I need $2500")
    before = agent.neg.current_offer
    agent.handle("no, come on")
    # It went through the engine rather than into the question branch.
    assert agent.neg.current_offer >= before
    assert agent.state.value in ("negotiate", "confirm_booking", "done")
