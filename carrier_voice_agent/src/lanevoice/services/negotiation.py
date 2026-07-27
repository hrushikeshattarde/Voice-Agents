"""
Negotiation engine (PRD §5 / §9.4).

Strategy — negotiate the way a real carrier rep does: anchor at the FLOOR (Load
Board Rate), make the carrier come to you, then CLOSE.

  * Opens at the floor (open_rate) and HOLDS: restates its own number and asks
    the carrier how close they can come to it. It never bids against itself.
  * RECIPROCITY — it moves only when the carrier moves, and it gives back a
    FRACTION of what the carrier just gave (default half). A $200 concession
    earns $100 back; a $30 nibble earns $15. Repeating the same number, or just
    saying "no", earns nothing.
  * SPLIT AND BOOK — once the gap is small, it stops trading nickels and offers
    to meet in the middle, which is how reps actually close.
  * CLOSE ENOUGH — inside `settle_gap` it just takes the carrier's number.
    Nobody loses a load arguing over $50.
  * DISCRETION vs MAX BUY — the agent commits on its own only up to
    `max_offer` (a fraction of the floor -> Max Buy span). A carrier who digs in
    ABOVE that but still within Max Buy isn't refused and isn't paid on the
    spot: it goes to a human, exactly like a rep asking their manager.
  * NO WALKING AWAY FROM A DEAL WE CAN SIGN — after best-and-final, a carrier
    holding at or below `max_offer` gets booked at their number. Losing a load
    over the last $100 is worse than paying it.
  * A carrier who NEVER moves doesn't get handed the best number: their
    best-and-final is only a partial stretch. Movement is what buys movement.

No offer ever exceeds Max Buy. accept/reject is decided ONLY here; the LLM
cannot influence it. Fully unit-testable.
"""

from __future__ import annotations

from lanevoice.domain.models import Decision, Load, NegotiationResult

_MIN_STEP = 10           # floor on a single upward nudge
_MIN_CARRIER_MOVE = 25   # a drop smaller than this isn't a real concession


def _round(amount: float) -> int:
    """Rates get spoken out loud — keep them to the nearest $5."""
    return int(round(amount / 5.0) * 5)


class NegotiationEngine:
    def __init__(
        self,
        load: Load,
        *,
        max_rounds: int = 8,
        buffer: float = 0.0,
        reciprocity: float = 0.5,
        discretion_rate: float = 0.6,
        settle_gap_rate: float = 0.10,
        split_gap_rate: float = 0.30,
        stonewall_final_rate: float = 0.5,
        max_holds: int = 2,
    ):
        self.load = load
        self.max_rounds = max_rounds
        self.max_holds = max_holds
        self.reciprocity = reciprocity
        self.stonewall_final_rate = stonewall_final_rate

        self.ceiling = load.ceiling_rate            # Max Buy — the hard cap
        self.fraud_low = load.fraud_low_rate
        self.floor = load.open_rate                 # Load Board Rate — we anchor here
        self.agent_max = max(self.floor, self.ceiling - buffer)   # <= Max Buy
        span = self.agent_max - self.floor

        # The most the agent will put on the table by itself. Between here and
        # Max Buy is a human's call — the bot doesn't spend that on its own.
        self.max_offer = max(self.floor, _round(min(
            self.agent_max, self.floor + span * discretion_rate)))
        # Gap we won't haggle over — just book it.
        self.settle_gap = max(_MIN_STEP, _round(span * settle_gap_rate))
        # Gap that's close enough to reach for the split-the-difference close.
        self.split_gap = max(self.settle_gap, _round(span * split_gap_rate))

        self.round = 0
        self.current_offer = self.floor
        self.holds = 0                               # pushes with no carrier movement
        self.concessions = 0                         # steps we've already given
        self.final_made = False                      # best-and-final is on the table
        self.split_made = False                      # we've played the split close
        self.carrier_moved = False                   # they've conceded at least once
        self.asks: list[float] = []                  # the carrier's ask history
        self.offers_made: list[float] = []

    def evaluate(self, carrier_ask: float) -> NegotiationResult:
        """`carrier_ask` = the rate the carrier wants to be PAID."""
        self.round += 1

        if carrier_ask < self.fraud_low:
            return NegotiationResult(decision=Decision.REVIEW, reason="suspiciously_low")

        # Carrier came down to (or below) our current offer -> take it.
        if carrier_ask <= self.current_offer:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)

        # Best-and-final is already out there: this is the endgame, not another round.
        if self.final_made:
            return self._resolve(carrier_ask)

        previous_ask = self.asks[-1] if self.asks else None
        self.asks.append(carrier_ask)
        drop = previous_ask - carrier_ask if previous_ask is not None else 0.0
        moved_down = drop >= _MIN_CARRIER_MOVE
        self.carrier_moved = self.carrier_moved or moved_down
        gap = carrier_ask - self.current_offer

        # Pennies apart, and it's a number we can sign off on -> book it. A rep
        # doesn't blow up a load over the last $50.
        if gap <= self.settle_gap and carrier_ask <= self.max_offer:
            return NegotiationResult(
                decision=Decision.ACCEPT, rate=carrier_ask, reason="close_enough")

        last_round = self.round >= self.max_rounds

        # They haven't given us anything -> hold our number and push them toward
        # it. Conceding here would just be bidding against ourselves.
        if not moved_down:
            self.holds += 1
            if self.holds <= self.max_holds and not last_round:
                return NegotiationResult(
                    decision=Decision.HOLD,
                    rate=self.current_offer,
                    hold_number=self.holds,
                    reason="carrier_has_not_moved",
                )
            return self._final_offer(carrier_ask)

        # They moved toward us.
        self.holds = 0
        if last_round:
            return self._final_offer(carrier_ask)
        # Close enough to stop trading nickels — offer to split it and book.
        if gap <= self.split_gap and not self.split_made:
            return self._split(carrier_ask)
        return self._step_up(carrier_ask, drop)

    # -- offer mechanics ---------------------------------------------------- #
    def _raise_to(self, carrier_ask: float, step: int, *,
                  is_final: bool = False, is_split: bool = False) -> NegotiationResult:
        # Never above what we can commit to, and never above their own ask.
        proposed = min(self.current_offer + step, self.max_offer, carrier_ask)
        self.current_offer = proposed
        self.concessions += 1
        self.offers_made.append(proposed)
        # Our raised offer covers their ask -> settle at their number.
        if carrier_ask <= proposed:
            return NegotiationResult(decision=Decision.ACCEPT, rate=carrier_ask)
        return NegotiationResult(
            decision=Decision.COUNTER, rate=proposed,
            is_final=is_final or proposed >= self.max_offer,
            is_split=is_split,
        )

    def _step_up(self, carrier_ask: float, drop: float) -> NegotiationResult:
        """Give back a fraction of what they just gave — never more."""
        if self.current_offer >= self.max_offer:
            return self._final_offer(carrier_ask)
        step = min(max(_round(drop * self.reciprocity), _MIN_STEP), int(drop))
        return self._raise_to(carrier_ask, step)

    def _split(self, carrier_ask: float) -> NegotiationResult:
        """The classic close: meet in the middle and ask for the load."""
        target = _round(min((self.current_offer + carrier_ask) / 2, self.max_offer))
        if target <= self.current_offer:
            return self._final_offer(carrier_ask)
        self.split_made = True
        return self._raise_to(carrier_ask, target - int(self.current_offer), is_split=True)

    def _final_offer(self, carrier_ask: float) -> NegotiationResult:
        """Out of holds or out of rounds: the best-and-final.

        A carrier who has been working with us gets our real best number. A
        carrier who has not moved at all gets a partial stretch — stonewalling
        must not pay better than negotiating.
        """
        room = self.max_offer - self.current_offer
        if room <= 0:
            return self._resolve(carrier_ask)
        step = int(room) if self.carrier_moved else max(
            _round(room * self.stonewall_final_rate), _MIN_STEP)
        self.final_made = True
        return self._raise_to(carrier_ask, step, is_final=True)

    def _resolve(self, carrier_ask: float) -> NegotiationResult:
        """They're still above us after best-and-final. Take it, escalate it, or
        let it go — but never walk away from a rate we're cleared to pay."""
        if carrier_ask <= self.max_offer:
            return NegotiationResult(
                decision=Decision.ACCEPT, rate=carrier_ask,
                reason="closed_at_carrier_number")
        if carrier_ask <= self.ceiling:
            # Inside Max Buy but above what the agent spends on its own — a
            # human decides, the same way a rep checks with their manager.
            return NegotiationResult(
                decision=Decision.ESCALATE, rate=carrier_ask,
                reason="above_agent_authority", final_offer=self.current_offer,
                within_ceiling=True)
        return NegotiationResult(
            decision=Decision.NO_DEAL, reason="above_max_buy",
            final_offer=self.current_offer, within_ceiling=False)
