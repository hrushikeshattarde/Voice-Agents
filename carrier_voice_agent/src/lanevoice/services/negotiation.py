"""
Negotiation engine (PRD §5 / §9.4).

Negotiates like a human rep, not a metronome:
  * Opens at the advertised rate.
  * HOLDS FIRM once on a high ask (pushes back, restates the opening).
  * Then makes DECREASING concessions along a ladder that converges to its cap
    (ceiling - buffer) — e.g. $2000 -> $2175 -> $2280 -> "$2350, that's my best".
  * If the carrier's ask drops within reach, it settles there immediately.
  * Walks away (no deal) once the ladder / patience is spent.
Suspiciously cheap asks are a fraud tripwire.

This is the ONLY place accept/reject is decided, against the live cap — the LLM
cannot influence it. Pure and fully unit-testable.
"""

from __future__ import annotations

from lanevoice.domain.models import Decision, Load, NegotiationResult

# Concession ladder as fractions of the room between the opening and the cap.
# Fewer, larger, decreasing moves — how a real broker walks a price up.
_LADDER_FRACTIONS = (0.5, 0.8, 1.0)


class NegotiationEngine:
    def __init__(self, load: Load, *, max_rounds: int = 6, buffer: float = 150.0):
        self.load = load
        self.max_rounds = max_rounds
        self.ceiling = load.ceiling_rate
        self.fraud_low = load.fraud_low_rate
        self.agent_max = max(load.open_rate, load.ceiling_rate - buffer)

        room = max(0.0, self.agent_max - load.open_rate)
        self._ladder = sorted(
            {round(load.open_rate + room * f) for f in _LADDER_FRACTIONS
             if load.open_rate + room * f > load.open_rate}
        )
        self._rung = 0

        self.round = 0
        self.current_offer = load.open_rate
        self.held_firm = False
        self.offers_made: list[float] = []

    def evaluate(self, carrier_ask: float) -> NegotiationResult:
        """`carrier_ask` = the rate the carrier wants to be PAID."""
        self.round += 1

        # Fraud tripwire: absurdly cheap -> double-brokering / no-show risk.
        if carrier_ask < self.fraud_low:
            return NegotiationResult(decision=Decision.REVIEW, reason="suspiciously_low")

        # Carrier at/below what we already offer -> take it (cheap for us).
        if carrier_ask <= self.current_offer:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        # First push-back on a high ask: hold firm, restate the opening.
        if not self.held_firm:
            self.held_firm = True
            return NegotiationResult(decision=Decision.HOLD, rate=self.current_offer)

        # Out of ladder rungs or patience -> walk away at our last real offer.
        if self._rung >= len(self._ladder) or self.round >= self.max_rounds:
            return NegotiationResult(
                decision=Decision.NO_DEAL,
                reason="stalemate",
                final_offer=self.current_offer,
                within_ceiling=carrier_ask <= self.ceiling,
            )

        # Make the next (decreasing) concession up the ladder.
        proposed = self._ladder[self._rung]
        self._rung += 1
        self.current_offer = proposed
        self.offers_made.append(proposed)

        # If our raised offer meets their ask, settle at their (lower) number.
        if carrier_ask <= proposed:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        is_final = self._rung >= len(self._ladder)   # reached the cap
        return NegotiationResult(decision=Decision.COUNTER, rate=proposed, is_final=is_final)
