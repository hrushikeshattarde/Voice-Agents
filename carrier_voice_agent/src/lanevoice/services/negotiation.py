"""
Negotiation engine (PRD §5 / §9.4).

Behaves like a human rep: opens at the advertised rate, HOLDS FIRM on the first
high ask, then walks its offer UP by a small step, never exceeding
`ceiling - buffer`, and walks away (no deal) once it can't move further or
patience runs out. Suspiciously cheap asks are a fraud tripwire.

This is the ONLY place accept/reject is decided, against the live ceiling — the
LLM cannot influence it. Pure and fully unit-testable.
"""

from __future__ import annotations

from lanevoice.domain.models import Decision, Load, NegotiationResult


class NegotiationEngine:
    def __init__(
        self,
        load: Load,
        *,
        max_rounds: int = 6,
        buffer: float = 150.0,
        step_small: int = 25,
        step_big: int = 30,
    ):
        self.load = load
        self.max_rounds = max_rounds
        self.step_small = step_small
        self.step_big = step_big
        self.ceiling = load.ceiling_rate
        self.fraud_low = load.fraud_low_rate
        self.agent_max = max(load.open_rate, load.ceiling_rate - buffer)

        self.round = 0
        self.current_offer = load.open_rate
        self.held_firm = False
        self.offers_made: list[float] = []

    def _step(self, gap: float) -> int:
        return self.step_big if gap > 100 else self.step_small

    def evaluate(self, carrier_ask: float) -> NegotiationResult:
        """`carrier_ask` = the rate the carrier wants to be PAID."""
        self.round += 1

        # Fraud tripwire: absurdly cheap -> double-brokering / no-show risk.
        if carrier_ask < self.fraud_low:
            return NegotiationResult(decision=Decision.REVIEW, reason="suspiciously_low")

        # Carrier at/below our current offer -> book it (cheap for us).
        if carrier_ask <= self.current_offer:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        # First push-back on a high ask: hold firm, restate the opening.
        if not self.held_firm:
            self.held_firm = True
            return NegotiationResult(decision=Decision.HOLD, rate=self.current_offer)

        # Consider walking our offer up toward the cap.
        gap = carrier_ask - self.current_offer
        proposed = min(self.current_offer + self._step(gap), self.agent_max)
        moved = proposed > self.current_offer

        if carrier_ask > proposed and (
            not moved or proposed >= self.agent_max or self.round >= self.max_rounds
        ):
            return NegotiationResult(
                decision=Decision.NO_DEAL,
                reason="stalemate",
                final_offer=self.current_offer,      # last offer actually made
                within_ceiling=carrier_ask <= self.ceiling,
            )

        # Make the raised offer.
        self.current_offer = proposed
        self.offers_made.append(proposed)
        if carrier_ask <= proposed:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)
        return NegotiationResult(decision=Decision.COUNTER, rate=proposed)
