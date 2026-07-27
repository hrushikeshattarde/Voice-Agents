"""
Negotiation engine (PRD §5 / §9.4).

Negotiates like a human rep:
  * Opens at the advertised rate.
  * HOLDS FIRM once on a high ask.
  * Then SPLITS THE DIFFERENCE — meets the carrier partway toward their number
    (decreasing concessions), never crossing its cap (ceiling - buffer), so it
    doesn't just cave to the exact ask. Snaps to close when the gap is tiny.
  * Settles at the carrier's number once its offer reaches it.
  * Walks away (no deal) once concessions / patience are spent.
Suspiciously cheap asks are a fraud tripwire.

This is the ONLY place accept/reject is decided, against the live cap — the LLM
cannot influence it. Pure and fully unit-testable.
"""

from __future__ import annotations

from lanevoice.domain.models import Decision, Load, NegotiationResult

# Fraction of the remaining gap to concede on each round (decreasing in $ terms).
_CONCESSION_FRACTIONS = (0.5, 0.5, 1.0)
# If we're within this of the target, just close rather than nudge again.
_SNAP = 25


class NegotiationEngine:
    def __init__(self, load: Load, *, max_rounds: int = 6, buffer: float = 150.0):
        self.load = load
        self.max_rounds = max_rounds
        self.ceiling = load.ceiling_rate
        self.fraud_low = load.fraud_low_rate
        self.agent_max = max(load.open_rate, load.ceiling_rate - buffer)

        self.round = 0
        self.current_offer = load.open_rate
        self.held_firm = False
        self._concession = 0
        self.offers_made: list[float] = []

    def evaluate(self, carrier_ask: float) -> NegotiationResult:
        """`carrier_ask` = the rate the carrier wants to be PAID."""
        self.round += 1

        if carrier_ask < self.fraud_low:
            return NegotiationResult(decision=Decision.REVIEW, reason="suspiciously_low")

        # Carrier at/below what we already offer -> take it.
        if carrier_ask <= self.current_offer:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        # First push-back on a high ask: hold firm.
        if not self.held_firm:
            self.held_firm = True
            return NegotiationResult(decision=Decision.HOLD, rate=self.current_offer)

        # Out of concessions or patience -> walk away at our last real offer.
        if self._concession >= len(_CONCESSION_FRACTIONS) or self.round >= self.max_rounds:
            return NegotiationResult(
                decision=Decision.NO_DEAL,
                reason="stalemate",
                final_offer=self.current_offer,
                within_ceiling=carrier_ask <= self.ceiling,
            )

        # Split the difference toward the carrier's number, but never past our cap.
        target = min(carrier_ask, self.agent_max)
        if target <= self.current_offer:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        remaining = target - self.current_offer
        frac = _CONCESSION_FRACTIONS[self._concession]
        self._concession += 1
        step = remaining if remaining <= _SNAP else round(remaining * frac)
        proposed = min(self.current_offer + step, self.agent_max)
        self.current_offer = proposed
        self.offers_made.append(proposed)

        # If our raised offer meets their ask, settle at their number.
        if carrier_ask <= proposed:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        is_final = (
            proposed >= self.agent_max
            or self._concession >= len(_CONCESSION_FRACTIONS)
        )
        return NegotiationResult(decision=Decision.COUNTER, rate=proposed, is_final=is_final)
