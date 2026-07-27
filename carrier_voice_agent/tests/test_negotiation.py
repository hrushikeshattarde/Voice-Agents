"""Negotiation engine — the safety-critical unit tests (PRD §9.4)."""

import pytest

from lanevoice.domain.models import Decision
from lanevoice.services import NegotiationEngine


@pytest.fixture
def engine(repo):
    # L1001: open 2000, ceiling 2500, fraud_low 1400 -> agent_max = 2350
    load = repo.get_load("L1001")
    return NegotiationEngine(load, max_rounds=6, buffer=150, step_small=25, step_big=30)


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


def test_walk_up_then_accept(engine):
    assert engine.evaluate(2100).decision == Decision.HOLD
    step = engine.evaluate(2100)            # now walk up
    assert step.decision == Decision.COUNTER
    assert step.rate == 2025
    done = engine.evaluate(2025)            # carrier meets our raised offer
    assert done.decision == Decision.ACCEPT


def test_never_offers_above_cap_and_walks_away():
    # Force a tight patience so the stalemate is reached quickly.
    import pathlib
    import tempfile

    from lanevoice.db import Database, Repository

    tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    repo = Repository(Database(tmp))
    repo._db.reset(seed=True)
    load = repo.get_load("L1003")           # open 900, ceiling 1250 -> cap 1100
    eng = NegotiationEngine(load, max_rounds=3, buffer=150, step_small=25, step_big=30)

    eng.evaluate(1500)                       # HOLD
    eng.evaluate(1500)                       # COUNTER (walk up)
    final = eng.evaluate(1500)               # patience spent -> NO_DEAL
    assert final.decision == Decision.NO_DEAL
    assert final.within_ceiling is False     # 1500 is above the 1250 ceiling
    # Every offer the agent made stayed at or below the cap.
    assert all(o <= eng.agent_max for o in eng.offers_made)
