"""
CarrierSalesAgent — the call state machine (PRD §3).

Independent of audio I/O: feed it carrier utterances, get back agent speech.
The LLM (phraser) only rewords; every decision comes from the services.

    GREETING -> IDENTIFY_LOAD -> VERIFY_CARRIER -> STATE_PRICE
             -> NEGOTIATE -> DONE (booked | transferred | rejected | no_deal)
"""

from __future__ import annotations

import enum
import uuid

from lanevoice import parsing
from lanevoice.db.repository import Repository
from lanevoice.domain.models import (
    CallOutcome,
    Decision,
    OfferParty,
    VerificationAction,
)
from lanevoice.services import (
    CarrierVerificationService,
    LoadService,
    NegotiationEngine,
    TransferService,
)
from lanevoice.settings import Settings, get_settings
from lanevoice.voice.phraser import Phraser

# The agent's persona — used in greetings and passed to the phrasing LLM so the
# whole call sounds like one consistent human rep.
REP_NAME = "Alex"

_ACCEPT_WORDS = (
    "that works", "that'll work", "works for me", "deal", "i'll take it",
    "sounds good", "book it", "agreed", "accept", "perfect", "yes", "yeah",
    "yep", "yup", "ok", "okay", "sure", "fine", "let's do it", "you got it",
)
_HUMAN_WORDS = (
    "talk to a human", "speak to someone", "representative", "a rep",
    "real person", "agent please",
)
# Rejections with no new number — the carrier is holding, so we make our next move.
_REJECT_WORDS = (
    "no", "nope", "nah", "can't", "cannot", "too low", "not enough",
    "come on", "forget it", "pass", "higher", "more than that", "no way",
)


class CallState(str, enum.Enum):
    GREETING = "greeting"
    IDENTIFY_LOAD = "identify_load"
    VERIFY_CARRIER = "verify_carrier"
    STATE_PRICE = "state_price"
    NEGOTIATE = "negotiate"
    DONE = "done"


class CarrierSalesAgent:
    def __init__(
        self,
        repo: Repository,
        phraser: Phraser | None = None,
        settings: Settings | None = None,
    ):
        self._repo = repo
        self._phraser = phraser
        self._settings = settings or get_settings()

        self._loads = LoadService(repo)
        self._verifier = CarrierVerificationService(repo)
        self._transfers = TransferService(repo)

        self.call_id = f"CALL-{uuid.uuid4().hex[:8]}"
        self.state = CallState.GREETING
        self.load = None
        self.carrier = None
        self.neg: NegotiationEngine | None = None
        self._last_ask: float | None = None
        self.transcript: list[tuple[str, str]] = []
        self.outcome: CallOutcome | None = None
        repo.start_call(self.call_id)

    # -- phrasing helpers --------------------------------------------------- #
    def _recent_dialogue(self, n: int = 4) -> str:
        speaker = {"agent": f"You ({REP_NAME})", "carrier": "Carrier"}
        return "\n".join(
            f"{speaker.get(who, who)}: {line}" for who, line in self.transcript[-n:]
        )

    def _say(self, fallback: str, instruction: str | None = None,
             context: str = "") -> str:
        text = fallback
        if self._phraser is not None and instruction is not None:
            convo = self._recent_dialogue()
            full_context = f"Recent conversation:\n{convo}\n\n{context}".strip() \
                if convo else context
            try:
                text = self._phraser.phrase(instruction, full_context)
            except Exception:  # noqa: BLE001 - phrasing must never break a call
                text = fallback
        self.transcript.append(("agent", text))
        return text

    def _log_user(self, text: str) -> None:
        self.transcript.append(("carrier", text))

    # -- entry points ------------------------------------------------------- #
    def greeting(self) -> str:
        self.state = CallState.IDENTIFY_LOAD
        return self._say(
            f"Thanks for calling the load desk, this is {REP_NAME} — heads up, the call "
            "may be recorded for quality. What load can I get you on? You got a load ID "
            "or a lane for me?",
            instruction=f"You are {REP_NAME}, a carrier sales rep answering an inbound "
            "call. Greet warmly and naturally, give a quick one-line 'call may be "
            "recorded' note, then ask which load they're calling about (load ID or lane).",
        )

    def handle(self, user_text: str) -> str:
        self._log_user(user_text)
        handler = {
            CallState.IDENTIFY_LOAD: self._identify_load,
            CallState.VERIFY_CARRIER: self._verify_carrier,
            CallState.STATE_PRICE: self._negotiate,
            CallState.NEGOTIATE: self._negotiate,
            CallState.DONE: lambda _t: "This call has ended. Goodbye.",
        }.get(self.state, self._identify_load)
        return handler(user_text)

    # -- Step 2: identify load --------------------------------------------- #
    def _identify_load(self, text: str) -> str:
        load_id = parsing.extract_load_id(text)
        if not load_id:
            return self._say(
                "I didn't catch a load ID. Could you repeat it? For example, L1001.",
                instruction="Politely say you didn't catch the load ID and ask them "
                "to repeat it, giving 'L1001' as an example format.",
            )
        result = self._loads.lookup(load_id)
        if not result.found:
            opens = ", ".join(self._loads.open_load_ids())
            return self._say(
                f"I couldn't find load {load_id}. Open loads right now are {opens}. "
                "Which one would you like?",
                instruction=f"Tell the caller load {load_id} was not found, then offer "
                f"the open loads: {opens}. Ask which they want.",
            )
        if not result.available:
            opens = ", ".join(self._loads.open_load_ids())
            return self._say(
                f"Sorry, load {load_id} is already covered. Other open loads: {opens}.",
                instruction=f"Tell the caller load {load_id} is already covered and "
                f"offer open loads: {opens}.",
            )

        self.load = result.load
        self.state = CallState.VERIFY_CARRIER
        ld = self.load
        return self._say(
            f"Got it — load {ld.load_id}, {ld.origin} to {ld.destination}, "
            f"picking up {ld.pickup_date}, {ld.equipment}. "
            "To move forward I'll need your MC or USDOT number.",
            instruction="Confirm the load back to the carrier, then ask for their MC "
            "or USDOT number.",
            context=f"Load {ld.load_id} {ld.origin}->{ld.destination} "
            f"pickup {ld.pickup_date} equip {ld.equipment}",
        )

    # -- Step 3: verify carrier -------------------------------------------- #
    def _verify_carrier(self, text: str) -> str:
        _, number = parsing.extract_mc_dot(text)
        if not number:
            return self._say(
                "I didn't get that. Please say your MC or USDOT number slowly.",
                instruction="Say you didn't catch the number and ask them to repeat "
                "their MC or USDOT number slowly.",
            )
        result = self._verifier.verify(number)

        if not result.verified:
            self._finish(CallOutcome.REJECTED)
            return self._say(
                "I'm not able to verify active authority and insurance on that number, "
                "so I can't discuss rate right now. I'm routing this to our team for "
                "review and someone will follow up. Thanks for calling.",
                instruction="Firmly but politely explain you cannot verify active "
                "authority/insurance so you cannot discuss rate, and that it is being "
                "routed to a human for review. Do not reveal internal fraud logic.",
            )

        self.carrier = result.carrier
        if result.action == VerificationAction.HUMAN_REVIEW:
            self._repo.log_note(
                self.call_id,
                f"Carrier {self.carrier.legal_name} verified but flagged "
                f"{list(result.risk_flags)} — routed to a rep.",
            )
            self._transfer(reason="verification_review")
            return self._say(
                "Thanks. I want to get you to a rep directly to finish verification — "
                "one moment while I connect you.",
                instruction="Tell the carrier you're connecting them to a human rep to "
                "finish verification. Keep it smooth and non-accusatory.",
            )

        self.neg = NegotiationEngine(
            self.load,
            max_rounds=self._settings.max_negotiation_rounds,
            buffer=self._settings.negotiation_buffer,
        )
        self.state = CallState.STATE_PRICE
        opening = int(self.neg.current_offer)
        self._repo.log_offer(self.call_id, 0, OfferParty.AGENT, opening)
        return self._say(
            f"Perfect, you're all set, {self.carrier.legal_name}. I've got this one at "
            f"${opening} — how's that sound?",
            instruction=f"Tell the carrier they're verified, then float an opening offer of "
            f"${opening} and ask if it works. Casual and confident.",
            context=f"Carrier {self.carrier.legal_name}. Your opening offer is ${opening}.",
        )

    # -- Steps 4/5: negotiate ---------------------------------------------- #
    def _negotiate(self, text: str) -> str:
        lowered = text.lower()
        money = parsing.extract_money(text)

        # Explicit "yes" with no number -> accept the offer on the table.
        if money is None and any(w in lowered for w in _ACCEPT_WORDS):
            return self._book(self.neg.current_offer)
        if any(w in lowered for w in _HUMAN_WORDS):
            return self._transfer_and_say(reason="carrier_request")

        if money is not None:
            self._last_ask = money
            ask = money
            self._repo.log_offer(
                self.call_id, self.neg.round + 1, OfferParty.CARRIER, money)
        elif self._last_ask is not None and any(w in lowered for w in _REJECT_WORDS):
            # "No" / "come on" with no new number -> they're holding; make our move.
            ask = self._last_ask
        else:
            return self._say(
                f"What are you looking to get on {self.load.load_id}?",
                instruction="Ask the carrier, naturally, what rate they need for this load.",
            )

        self.state = CallState.NEGOTIATE
        return self._apply_negotiation(self.neg.evaluate(ask), ask)

    def _apply_negotiation(self, result, ask: float) -> str:
        if result.decision == Decision.ACCEPT:
            return self._book(result.rate)

        if result.decision == Decision.REVIEW:
            self._repo.log_note(
                self.call_id,
                f"Suspiciously low ask ${int(ask)} on {self.load.load_id} "
                "(fraud tripwire) — routed to review.",
            )
            return self._transfer_and_say(reason="fraud_review")

        if result.decision == Decision.NO_DEAL:
            return self._no_deal(ask, result)

        if result.decision == Decision.HOLD:
            rate = int(result.rate)
            self._repo.log_offer(self.call_id, self.neg.round, OfferParty.AGENT, rate)
            return self._say(
                f"Oof, ${int(ask)} is a tough one for this lane — I'm sitting at ${rate} "
                "right now. Any way you can work with me there?",
                instruction=f"The carrier wants ${int(ask)}, which is more than you can pay. "
                f"Push back warmly, react to their number, and hold at your current offer "
                f"of ${rate}. Never reveal your max. Sound like a real rep, not scripted.",
                context=f"Hold firm at ${rate}. Never reveal your ceiling/max.",
            )

        # COUNTER — a real concession up the ladder
        rate = int(result.rate)
        self._repo.log_offer(self.call_id, self.neg.round, OfferParty.AGENT, rate)
        if result.is_final:
            return self._say(
                f"Alright — tell you what, I'll stretch to ${rate}. That's honestly the "
                "best I can do on this one. Can we make it happen?",
                instruction=f"Make your FINAL, best offer of ${rate}. Convey warmly but "
                "firmly that this is as high as you can go, and ask them to take it. Do "
                "NOT say it's a hard cap or reveal any internal number.",
                context=f"Final best offer ${rate}. Never call it your ceiling/max.",
            )
        return self._say(
            f"Okay, I can move up to ${rate} — that get us closer?",
            instruction=f"Acknowledge their push, then concede up to ${rate} like you're "
            "working with them to close it. Vary your wording; ask if that works.",
            context=f"Your improved offer is ${rate}. Never reveal your max.",
        )

    # -- Step 6a: book ------------------------------------------------------ #
    def _book(self, rate: float) -> str:
        if rate > self.neg.ceiling:   # final server-side guard (defense in depth)
            return self._transfer_and_say(reason="ceiling_guard")
        self._repo.book_load(self.load.load_id)
        self._repo.log_offer(self.call_id, self.neg.round, OfferParty.AGENT, rate)
        self._finish(CallOutcome.BOOKED)
        return self._say(
            f"Done — you're booked on load {self.load.load_id} at ${int(rate)}. "
            "Rate confirmation is on its way to you. Thanks, and drive safe.",
            instruction=f"Confirm the booking on load {self.load.load_id} at ${int(rate)}, "
            "mention a rate confirmation is coming, and close warmly.",
        )

    # -- Step 6b: transfer -------------------------------------------------- #
    def _transfer(self, reason: str) -> object:
        resolution = self._transfers.resolve(self.load)
        self._finish(CallOutcome.TRANSFERRED, rep_id=resolution.rep.rep_id
                     if resolution.rep else None)
        return resolution

    def _transfer_and_say(self, reason: str) -> str:
        resolution = self._transfer(reason)
        if resolution.rep is None:
            return self._say(
                "Everyone's on the phone right now. I've logged a callback task and a "
                "rep will call you right back. Thanks for your patience.",
                instruction="Explain no rep is available, that you've logged a callback, "
                "and someone will call them back shortly.",
            )
        return self._say(
            f"Let me connect you with {resolution.rep.name}, who handles this load. "
            "One moment.",
            instruction=f"Tell the carrier you're connecting them to {resolution.rep.name} "
            "who handles this load.",
        )

    # -- No deal: walked up, still apart -> note + decline + end ------------ #
    def _no_deal(self, ask: float, result) -> str:
        offers = ", ".join(f"${int(o)}" for o in self.neg.offers_made) or "n/a"
        followup = (
            "Within our ceiling — a rep could still close it using the reserved buffer."
            if result.within_ceiling
            else "Above our ceiling — not workable."
        )
        self._repo.log_note(
            self.call_id,
            f"NO DEAL on {self.load.load_id} ({self.load.origin} -> "
            f"{self.load.destination}). Carrier {self.carrier.legal_name} "
            f"(USDOT {self.carrier.usdot_number}) held at ${int(ask)}. We opened at "
            f"${int(self.neg.load.open_rate)} and walked up to ${int(result.final_offer)} "
            f"(offers: {offers}) over {self.neg.round} rounds; no agreement. "
            f"{followup} Call ended by agent.",
        )
        self._finish(CallOutcome.NO_DEAL)
        return self._say(
            "I've come up as far as I can on this one and we're still apart, so I won't "
            "be able to make it work today. I'll note it down — thanks for calling, and "
            "let's catch the next one. Take care.",
            instruction="Politely say you've come up as far as you can and you're still "
            "apart, so you can't make a deal today. Do NOT reveal any numbers. Close "
            "warmly. 1-2 sentences.",
        )

    # -- persistence -------------------------------------------------------- #
    def _finish(self, outcome: CallOutcome, rep_id: str | None = None) -> None:
        self.state = CallState.DONE
        self.outcome = outcome
        self._repo.end_call(
            self.call_id,
            self.load.load_id if self.load else None,
            self.carrier.usdot_number if self.carrier else None,
            outcome.value,
            self.transcript,
        )
        if rep_id and outcome == CallOutcome.TRANSFERRED:
            self._repo.log_transfer(self.call_id, rep_id, "connected")

    def summary(self) -> dict:
        return {
            "call_id": self.call_id,
            "outcome": self.outcome.value if self.outcome else None,
            "load_id": self.load.load_id if self.load else None,
            "carrier": self.carrier.legal_name if self.carrier else None,
            "turns": len(self.transcript),
        }
