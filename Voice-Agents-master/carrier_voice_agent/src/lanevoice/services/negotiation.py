"""
Negotiation engine (PRD §5 / §9.4).

Strategy — negotiate the way a real carrier rep does: anchor at the FLOOR (Load
Board Rate), make the CARRIER do the walking, then CLOSE in one move.

  * Opens at the floor (open_rate) and HOLDS: restates its own number and asks
    the carrier how close they can come to it. It never bids against itself.
  * NO INCREMENTAL LADDER. The agent does not answer a carrier's concession with
    a concession of its own. Movement from the carrier earns another ASK, not a
    counter-offer — "that's better, but I'm at $2000, how close can you get?"
    A carrier who is still coming down is a carrier who can come down further,
    and every dollar we don't spend answering them is margin.
  * PULL, don't pay (`max_pulls`) — each time they move we credit it, restate
    our number, and put the next move back on them. Only when they stop coming
    (or say the number is their best) does any new money go on the table.
  * ONE CLOSING MOVE — when it's finally time to spend, the agent makes a single
    decisive offer covering `reciprocity` of the remaining gap and asks for the
    load on the spot. No $50 ladders: reps close, they don't nibble.
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
        max_pulls: int = 2,
    ):
        self.load = load
        self.max_rounds = max_rounds
        self.max_holds = max_holds
        self.max_pulls = max_pulls
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
        self.pulls = 0                               # times we asked them to come closer
        self.concessions = 0                         # steps we've already given
        self.final_made = False                      # best-and-final is on the table
        self.split_made = False                      # we've played our closing move
        self.carrier_moved = False                   # they've conceded at least once
        self.carrier_final = False                   # they've called a number their best
        self.asks: list[float] = []                  # the carrier's ask history
        self.offers_made: list[float] = []

    def evaluate(self, carrier_ask: float,
                 carrier_final: bool = False) -> NegotiationResult:
        """`carrier_ask` = the rate the carrier wants to be PAID.

        `carrier_final` = they called this number their best ("that's all I can
        do", "I'm firm at 2400"). We stop asking them to come down — pushing a
        carrier who has already given their best just burns the call — and go
        straight to closing it or letting it go.
        """
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
        self.carrier_final = self.carrier_final or carrier_final
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
            if (self.holds <= self.max_holds and not last_round
                    and not self.carrier_final):
                return NegotiationResult(
                    decision=Decision.HOLD,
                    rate=self.current_offer,
                    hold_number=self.holds,
                    reason="carrier_has_not_moved",
                )
            # Straight to the best-and-final, NOT the closing offer: `_final_offer`
            # is the one that knows a carrier who never moved gets a partial
            # stretch. Stonewalling must not out-earn negotiating.
            return self._final_offer(carrier_ask)

        # They moved toward us. This is where a bot bids against itself and a rep
        # doesn't: their concession earns them another ASK, not a counter-offer.
        # We stay on our number and keep them walking.
        self.holds = 0
        if last_round or self.carrier_final:
            return self._close(carrier_ask)
        # Spend only once, and only when it actually closes the load: either
        # they're near enough to reach in one move, or we've pulled as often as
        # we're going to and it's time to put a number out.
        if not self.split_made and (gap <= self.split_gap
                                    or self.pulls >= self.max_pulls):
            return self._split(carrier_ask)
        self.pulls += 1
        return NegotiationResult(
            decision=Decision.PULL,
            rate=self.current_offer,
            pull_number=self.pulls,
            reason="carrier_moving_ask_for_more",
        )

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
        spent_it_all = proposed >= self.max_offer
        # Once we've spoken our top number there is nothing left to offer, so the
        # next turn is the endgame — never say "best I can do" and then move again.
        self.final_made = self.final_made or is_final or spent_it_all
        return NegotiationResult(
            decision=Decision.COUNTER, rate=proposed,
            is_final=is_final or spent_it_all,
            is_split=is_split,
        )

    def _close(self, carrier_ask: float) -> NegotiationResult:
        """Time to stop asking and put a number out: our one closing move if we
        still have it, otherwise the best-and-final."""
        if not self.split_made:
            return self._split(carrier_ask)
        return self._final_offer(carrier_ask)

    def _split(self, carrier_ask: float) -> NegotiationResult:
        """Our ONE closing move — cover a share of the remaining gap and ask for
        the load on the spot. `reciprocity` is that share: 0.5 meets them in the
        middle, lower closes firmer. This replaces the old $50-at-a-time ladder;
        after this, the only number left to say is our best-and-final."""
        gap = carrier_ask - self.current_offer
        target = _round(min(self.current_offer + gap * self.reciprocity, self.max_offer))
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
