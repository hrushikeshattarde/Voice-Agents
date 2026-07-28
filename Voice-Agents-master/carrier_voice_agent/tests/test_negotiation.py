"""Negotiation engine — the safety-critical unit tests (PRD §9.4)."""

import pathlib
import tempfile

import pytest

from lanevoice.db import Database, Repository
from lanevoice.domain.models import Decision
from lanevoice.services import NegotiationEngine


@pytest.fixture
def engine(repo):
    # L1001: floor 2000, Max Buy 2500, no buffer. Shipped defaults ->
    #   max_offer (own authority) 2300, settle_gap $50, split_gap $150,
    #   max_holds 2, max_pulls 2, reciprocity 0.5 (our one closing move covers
    #   half the remaining gap).
    load = repo.get_load("L1001")
    return NegotiationEngine(load, buffer=0)


def test_accept_when_carrier_at_or_below_our_offer(engine):
    result = engine.evaluate(1900)          # below our opening offer -> take it
    assert result.decision == Decision.ACCEPT
    assert result.rate == 1900


def test_fraud_low_is_reviewed(engine):
    result = engine.evaluate(900)           # < fraud_low 1400
    assert result.decision == Decision.REVIEW


def test_high_ask_holds_firm_first(engine):
    result = engine.evaluate(2400)
    assert result.decision == Decision.HOLD
    assert result.rate == 2000              # restates the opening, does not move


def test_a_gap_not_worth_haggling_over_is_just_booked(engine):
    """$50 apart on a $2000 load: a rep books it, they don't trade nickels."""
    result = engine.evaluate(2050)
    assert result.decision == Decision.ACCEPT
    assert result.rate == 2050
    assert result.reason == "close_enough"


def test_repeating_the_same_ask_earns_no_concession(engine):
    """A carrier who won't move gets our same number back — we never bid
    against ourselves (this is what a real rep does)."""
    first = engine.evaluate(2400)
    assert first.decision == Decision.HOLD
    assert first.hold_number == 1
    second = engine.evaluate(2400)           # they didn't budge, so neither do we
    assert second.decision == Decision.HOLD
    assert second.rate == 2000               # still our opening, no walk-up
    assert second.hold_number == 2
    assert engine.offers_made == []          # we put no new money on the table


def test_carrier_movement_earns_another_ask_not_a_counter(engine):
    """The core of the strategy: they came down $200 and we spend NOTHING. We
    restate our number and put the next move back on them — a carrier who is
    still walking can usually keep walking."""
    assert engine.evaluate(2500).decision == Decision.HOLD
    pull = engine.evaluate(2300)             # they came down $200
    assert pull.decision == Decision.PULL
    assert pull.pull_number == 1
    assert pull.rate == 2000                 # still our opening — we did not move
    assert engine.offers_made == []          # no new money on the table


def test_a_nibble_does_not_buy_a_counter_either(engine):
    """A $30 tickle used to buy a $15 step up. Now it buys another question."""
    engine.evaluate(2400)                    # HOLD
    result = engine.evaluate(2370)           # they gave up only $30
    assert result.decision == Decision.PULL
    assert result.rate == 2000
    assert engine.offers_made == []


def test_the_closing_move_comes_after_the_pulling(engine):
    """Once we've made them walk twice (max_pulls), we stop asking and make ONE
    decisive offer — not a ladder of $50 steps."""
    engine.evaluate(2500)                    # HOLD
    assert engine.evaluate(2300).decision == Decision.PULL   # pull 1
    assert engine.evaluate(2200).decision == Decision.PULL   # pull 2
    result = engine.evaluate(2150)           # out of pulls -> close it
    assert result.decision == Decision.COUNTER
    assert result.is_split
    assert result.rate == 2075               # half the remaining $150 gap
    assert engine.offers_made == [2075]      # exactly one offer, ever


def test_a_reachable_gap_is_closed_without_pulling_first(engine):
    """If their move lands close enough to close in one go, don't keep asking —
    reps close a $150 gap, they don't interrogate it."""
    engine.evaluate(2150)                    # HOLD (gap 150 == split_gap)
    result = engine.evaluate(2120)           # they move, still inside split_gap
    assert result.decision == Decision.COUNTER
    assert result.is_split
    assert result.rate == 2060               # 2000 + half of the $120 gap


def test_declaring_a_best_number_stops_the_pulling(engine):
    """'That's my best' is the signal to stop asking. Pressing a carrier who has
    already given their best just burns the call — we close or we let it go."""
    engine.evaluate(2500)                    # HOLD
    result = engine.evaluate(2300, carrier_final=True)
    assert result.decision == Decision.COUNTER   # straight to our closing move
    assert engine.pulls == 0                     # never asked them to come down again
    assert engine.carrier_final


def test_the_transcript_that_should_have_booked(engine):
    """The reported call: carrier walks 2500 -> 2200, which is inside Max Buy.
    That load gets covered — it must not end in a no-deal. Under the pull
    strategy we make them do the walking first and only offer once."""
    engine.evaluate(2500)                    # HOLD
    engine.evaluate(2500)                    # HOLD (still nothing from them)
    engine.evaluate(2400)                    # they move -> PULL, we spend nothing
    engine.evaluate(2300)                    # they move -> PULL again
    engine.evaluate(2200)                    # out of pulls -> our one offer, 2100
    engine.evaluate(2200)                    # they sit -> HOLD
    engine.evaluate(2200)                    # they sit -> HOLD
    final = engine.evaluate(2200)            # out of holds -> best-and-final
    assert final.decision == Decision.ACCEPT
    assert final.rate == 2200
    assert final.rate <= engine.ceiling
    assert len(engine.offers_made) <= 2      # one close + one best-and-final, no ladder


def test_books_when_carrier_comes_down_to_us(engine):
    engine.evaluate(2500)                    # HOLD
    held = engine.evaluate(2300)             # they moved -> we PULL, still at 2000
    dropped = engine.evaluate(held.rate - 50)      # carrier drops below our offer
    assert dropped.decision == Decision.ACCEPT
    assert dropped.rate == held.rate - 50    # book at the carrier's lower number


def test_stonewalling_earns_less_than_negotiating(engine):
    """A carrier who never moves gets a partial stretch, not our best number."""
    engine.evaluate(2400)                    # HOLD
    engine.evaluate(2400)                    # HOLD
    final = engine.evaluate(2400)            # out of holds -> best and final
    assert final.decision == Decision.COUNTER
    assert final.is_final
    assert final.rate == 2150                # half the room to 2300, not all of it
    assert final.rate < engine.max_offer


def test_firm_carrier_inside_max_buy_goes_to_a_human(engine):
    """Above what the agent spends on its own but under Max Buy: don't walk,
    don't cave — escalate, exactly like a rep checking with their manager."""
    for _ in range(3):
        engine.evaluate(2400)                # holds, then best-and-final at 2150
    result = engine.evaluate(2400)           # they're immovable at 2400
    assert result.decision == Decision.ESCALATE
    assert result.within_ceiling is True     # 2400 <= Max Buy 2500
    assert result.reason == "above_agent_authority"


def test_never_walks_away_from_a_rate_it_can_pay(engine):
    """Post-final, a carrier landing at or below our own authority gets booked.
    Losing the load over the last $100 is the expensive mistake."""
    for _ in range(3):
        engine.evaluate(2400)                # holds, then best-and-final at 2150
    assert engine.final_made
    result = engine.evaluate(2250)           # still above us, but we can pay it
    assert result.decision == Decision.ACCEPT
    assert result.rate == 2250
    assert result.rate <= engine.max_offer


def test_a_firm_carrier_is_not_ground_on_after_our_closing_offer(engine):
    """From a real call: carrier walked 2400 -> 2300 -> 2150, we closed at 2075,
    and they repeated 2150 three times while we pushed back twice. Once our offer
    is out and they've answered it, one push is fair — then take the load. The
    gap here is $75 on a 925-mile lane; grinding it is how you lose freight you
    were always cleared to sign."""
    assert engine.evaluate(2400).decision == Decision.HOLD
    assert engine.evaluate(2300).decision == Decision.PULL
    close = engine.evaluate(2150)
    assert close.is_split and close.rate == 2075
    assert engine.evaluate(2150).decision == Decision.HOLD      # one push back
    done = engine.evaluate(2150)                                 # not a second
    assert done.decision == Decision.ACCEPT
    assert done.rate == 2150
    assert done.rate <= engine.max_offer


def test_the_full_hold_budget_still_applies_before_we_have_offered(engine):
    """The shortened budget is specifically about grinding AFTER we've spent. A
    carrier stonewalling before any offer still gets pushed the full count."""
    assert engine.evaluate(2400).hold_number == 1
    assert engine.evaluate(2400).hold_number == 2                # max_holds = 2
    assert not engine.split_made
    assert engine.evaluate(2400).is_final                        # then best-and-final


def test_movement_resets_our_patience(engine):
    """Two holds don't burn the call down: a carrier who then starts moving gets
    asked to keep coming, not hit with a best-and-final ultimatum."""
    engine.evaluate(2500)                    # HOLD
    engine.evaluate(2500)                    # HOLD
    result = engine.evaluate(2400)           # they finally move
    assert result.decision == Decision.PULL
    assert not result.is_final
    assert engine.holds == 0


def test_above_max_buy_is_the_only_no_deal(engine):
    result = None
    saw_final = False
    for _ in range(12):
        result = engine.evaluate(3000)       # 3000 is above the Max Buy (2500)
        if result.decision == Decision.COUNTER and result.is_final:
            saw_final = True
        if result.decision == Decision.NO_DEAL:
            break
    assert saw_final                         # it made a best-and-final offer first
    assert result.decision == Decision.NO_DEAL
    assert result.within_ceiling is False    # 3000 > Max Buy 2500
    assert all(o <= engine.agent_max for o in engine.offers_made)
    # Running the clock down must NOT hand the carrier our maximum.
    assert max(engine.offers_made) < engine.agent_max


def test_offers_never_exceed_the_agents_own_authority(engine):
    """Whatever the carrier does, an unsupervised offer stays under max_offer,
    which itself stays under Max Buy."""
    ask = 2500
    for _ in range(10):
        result = engine.evaluate(ask)
        if result.decision in (Decision.NO_DEAL, Decision.ESCALATE, Decision.ACCEPT):
            break
        ask = max(2150, ask - 40)
    assert all(o <= engine.max_offer for o in engine.offers_made)
    assert engine.max_offer < engine.ceiling


def test_never_offers_above_max_buy():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    repo = Repository(Database(tmp))
    repo._db.reset(seed=True)
    load = repo.get_load("L1003")            # floor 900, Max Buy 1250
    eng = NegotiationEngine(load, max_rounds=3, buffer=0)

    for _ in range(6):
        result = eng.evaluate(1500)          # holds well above the cap
        if result.decision == Decision.NO_DEAL:
            break
    assert result.decision == Decision.NO_DEAL
    assert all(o <= eng.agent_max for o in eng.offers_made)


def test_full_authority_config_closes_at_max_buy():
    """discretion_rate=1.0 lets the agent close all the way to Max Buy itself —
    no escalation path, for desks that want the bot to own the whole range."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    repo = Repository(Database(tmp))
    repo._db.reset(seed=True)
    eng = NegotiationEngine(repo.get_load("L1001"), buffer=0, discretion_rate=1.0)
    assert eng.max_offer == eng.ceiling == 2500

    for _ in range(4):
        result = eng.evaluate(2450)
    assert result.decision == Decision.ACCEPT      # inside Max Buy -> booked, not escalated
    assert result.rate == 2450
