"""Negotiation engine — the safety-critical unit tests (PRD §9.4)."""

import pathlib
import tempfile

import pytest

from lanevoice.db import Database, Repository
from lanevoice.domain.models import Decision
from lanevoice.services import NegotiationEngine


@pytest.fixture
def engine(repo):
    # L1001: open 2000, ceiling 2500, buffer 150 -> cap 2350, ladder [2175, 2280, 2350]
    load = repo.get_load("L1001")
    return NegotiationEngine(load, max_rounds=6, buffer=150)


def test_accept_at_or_below_opening(engine):
    result = engine.evaluate(1900)          # below our opening offer
    assert result.decision == Decision.ACCEPT
    assert result.rate == 1900


def test_fraud_low_is_reviewed(engine):
    result = engine.evaluate(900)           # < fraud_low 1400
    assert result.decision == Decision.REVIEW


def test_high_ask_holds_firm_first(engine):
    result = engine.evaluate(2100)
    assert result.decision == Decision.HOLD
    assert result.rate == 2000              # restates the opening, does not move


def test_split_the_difference_then_accepts(engine):
    # Carrier holds at 2300 (within our 2350 cap): we meet them partway, not instantly.
    assert engine.evaluate(2300).decision == Decision.HOLD
    c1 = engine.evaluate(2300)
    assert (c1.decision, c1.rate) == (Decision.COUNTER, 2150)   # +50% of the gap
    c2 = engine.evaluate(2300)
    assert (c2.decision, c2.rate) == (Decision.COUNTER, 2225)   # +50% of remainder
    done = engine.evaluate(2300)            # our offer now reaches their number
    assert done.decision == Decision.ACCEPT
    assert done.rate == 2300


def test_final_offer_is_flagged(engine):
    engine.evaluate(3000)                    # HOLD  (3000 is above our 2350 cap)
    engine.evaluate(3000)                    # COUNTER 2175
    engine.evaluate(3000)                    # COUNTER 2263
    final = engine.evaluate(3000)            # COUNTER 2350 = the cap
    assert final.decision == Decision.COUNTER
    assert final.rate == 2350
    assert final.is_final is True
    assert engine.evaluate(3000).decision == Decision.NO_DEAL


def test_never_offers_above_cap_and_walks_away():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    repo = Repository(Database(tmp))
    repo._db.reset(seed=True)
    load = repo.get_load("L1003")            # open 900, ceiling 1250 -> cap 1100
    eng = NegotiationEngine(load, max_rounds=3, buffer=150)

    eng.evaluate(1500)                        # HOLD
    eng.evaluate(1500)                        # COUNTER
    final = eng.evaluate(1500)                # patience spent -> NO_DEAL
    assert final.decision == Decision.NO_DEAL
    assert final.within_ceiling is False      # 1500 is above the 1250 ceiling
    assert all(o <= eng.agent_max for o in eng.offers_made)
