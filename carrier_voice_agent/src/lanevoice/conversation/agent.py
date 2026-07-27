"""
CarrierSalesAgent — the call state machine (PRD §3).

Independent of audio I/O: feed it carrier utterances, get back agent speech.
The LLM (phraser) only rewords; every decision comes from the services.

    GREETING -> IDENTIFY_LOAD -> VERIFY_CARRIER -> STATE_PRICE
             -> NEGOTIATE -> DONE (booked | transferred | rejected | no_deal)
"""

from __future__ import annotations

import enum
import re
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
# At the booking-confirmation step: the carrier can't cover the pickup.
_CANNOT_COVER = (
    "can't", "cannot", "can not", "not available", "won't work", "wont work",
    "no truck", "different day", "nope", "not that day",
)


def _declines_requirements(text: str) -> bool:
    """True if the carrier says they CAN'T meet the load's special requirements."""
    low = text.lower()
    # Explicit inability first (catches "i can't", "not equipped", ...).
    if any(p in low for p in (
        "can't", "cannot", "can not", "not able", "unable", "won't", "wont",
        "don't have", "not equipped", "no truck", "negative", "nope",
    )):
        return True
    # A standalone "no" (but not "no problem" / "no worries").
    if re.search(r"\bno\b", low) and "no problem" not in low and "no worries" not in low:
        return True
    return False


class CallState(str, enum.Enum):
    GREETING = "greeting"
    IDENTIFY_LOAD = "identify_load"
    VERIFY_CARRIER = "verify_carrier"
    CHECK_REQUIREMENTS = "check_requirements"   # narrate load notes, confirm carrier can do it
    STATE_PRICE = "state_price"
    NEGOTIATE = "negotiate"
    CONFIRM_BOOKING = "confirm_booking"   # rate agreed; confirm pickup + rate con
    COLLECT_DETAILS = "collect_details"   # capture email + driver/truck for the con
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
        self._agreed_rate: float | None = None
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
            CallState.CHECK_REQUIREMENTS: self._check_requirements,
            CallState.STATE_PRICE: self._negotiate,
            CallState.NEGOTIATE: self._negotiate,
            CallState.CONFIRM_BOOKING: self._confirm_booking,
            CallState.COLLECT_DETAILS: self._collect_details,
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
            opens = self._loads.open_loads_summary()
            return self._say(
                f"I couldn't find load {load_id}. Open loads right now are {opens}. "
                "Which one would you like?",
                instruction=f"Tell the caller load {load_id} was not found, then offer "
                f"ONLY these open loads with their exact lanes: {opens}. Do not invent "
                "any lanes. Ask which they want.",
                context=f"Open loads (use these lanes exactly): {opens}",
            )
        if not result.posted:
            opens = self._loads.open_loads_summary()
            return self._say(
                f"Load {load_id} isn't posted right now, so I can't book that one. "
                f"Open loads are {opens}. Which would you like?",
                instruction=f"Tell the caller load {load_id} isn't posted/available right "
                f"now, then offer ONLY these open loads with their exact lanes: {opens}. "
                "Do not invent any lanes.",
                context=f"Open loads (use these lanes exactly): {opens}",
            )
        if not result.available:
            opens = self._loads.open_loads_summary()
            return self._say(
                f"Sorry, load {load_id} is already covered. Other open loads: {opens}.",
                instruction=f"Tell the caller load {load_id} is already covered, then "
                f"offer ONLY these open loads with their exact lanes: {opens}. Do not "
                "invent any lanes.",
                context=f"Open loads (use these lanes exactly): {opens}",
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

        # Not approved to work with Circle Logistics -> decline, don't book.
        if result.action == VerificationAction.DECLINE:
            self._repo.log_note(
                self.call_id,
                f"Carrier {self.carrier.legal_name} (USDOT {self.carrier.usdot_number}) "
                "is not approved to work with Circle Logistics — declined.",
            )
            self._finish(CallOutcome.REJECTED)
            return self._say(
                "I'm sorry, but it looks like we're not set up to work with your company "
                "on our end, so I won't be able to book this one. Thanks for calling.",
                instruction="Politely tell the carrier your brokerage isn't set up to work "
                "with their company, so you can't book with them. Do not explain why or "
                "reveal internal lists. Keep it brief and professional.",
            )

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

        # Approved & verified. If the load has special requirements, read them out
        # and make sure the carrier can do it BEFORE talking rate.
        if self.load.notes:
            self.state = CallState.CHECK_REQUIREMENTS
            return self._say(
                f"You're good to go, {self.carrier.legal_name}. One thing on this load — "
                f"{self.load.notes} Can you handle that?",
                instruction=f"Confirm the carrier {self.carrier.legal_name} is verified "
                "(by company name, never the MC number), then read them the load's "
                f"special requirements verbatim: \"{self.load.notes}\" and ask if they can "
                "do it.",
                context=f"Carrier {self.carrier.legal_name}. Requirements: {self.load.notes}",
            )
        return self._present_offer()

    def _present_offer(self) -> str:
        """Set up negotiation and make the opening offer."""
        self.neg = NegotiationEngine(
            self.load,
            max_rounds=self._settings.max_negotiation_rounds,
            buffer=self._settings.negotiation_buffer,
        )
        self.state = CallState.STATE_PRICE
        opening = int(self.neg.current_offer)
        self._repo.log_offer(self.call_id, 0, OfferParty.AGENT, opening)
        lane = f"{self.load.origin.split(',')[0]} to {self.load.destination.split(',')[0]}"
        return self._say(
            f"Alright, {self.carrier.legal_name} — nice lane, {lane}. I've got this one "
            f"at ${opening}. That work?",
            instruction=f"Float the opening offer of ${opening} for the {lane} load and "
            "ask if it works. Add a quick bit of lane rapport. Do not read back the MC "
            "number.",
            context=f"Carrier {self.carrier.legal_name}. Lane {lane}. Opening offer ${opening}.",
        )

    # -- Load requirements gate (PRD step: narrate notes, confirm) --------- #
    def _check_requirements(self, text: str) -> str:
        if _declines_requirements(text):
            self._repo.log_note(
                self.call_id,
                f"Carrier {self.carrier.legal_name} can't meet the requirements on "
                f"{self.load.load_id} ({self.load.notes!r}) — not booked.",
            )
            self._finish(CallOutcome.NO_DEAL)
            return self._say(
                "No worries — since you can't cover those requirements, I can't book this "
                "one with you today. Thanks for calling, and let's catch the next one.",
                instruction="Politely say that since they can't meet the load's "
                "requirements you can't book it with them, and close warmly. 1-2 sentences.",
            )
        return self._present_offer()

    # -- Steps 4/5: negotiate ---------------------------------------------- #
    def _negotiate(self, text: str) -> str:
        lowered = text.lower()
        money = parsing.extract_money(text)

        # Explicit "yes" with no number -> accept the offer on the table.
        if money is None and any(w in lowered for w in _ACCEPT_WORDS):
            return self._propose_booking(self.neg.current_offer)
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
            return self._propose_booking(result.rate)

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
                f"Yeah, ${int(ask)}'s a reach for me on this one — I'm working with ${rate} "
                "right now. Can you help me out and get closer to that?",
                instruction=f"The carrier wants ${int(ask)}. Don't call their number "
                "too-high or 'above market' (you don't want to bluff a number you might "
                f"pay). Just say where you're at — ${rate} — and nudge them toward it. "
                "Blunt, warm, transactional; never reveal your max.",
                context=f"You're working with ${rate}. Never reveal your ceiling/max.",
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

    # -- Step 6a: agree on rate -> confirm operations before booking -------- #
    def _propose_booking(self, rate: float) -> str:
        if rate > self.neg.ceiling:   # final server-side guard (defense in depth)
            return self._transfer_and_say(reason="ceiling_guard")
        self._agreed_rate = rate
        self.state = CallState.CONFIRM_BOOKING
        self._repo.log_offer(self.call_id, self.neg.round, OfferParty.AGENT, rate)
        ld = self.load
        city = ld.origin.split(",")[0]
        return self._say(
            f"Alright, ${int(rate)} it is. Can you cover the pickup in {city} on "
            f"{ld.pickup_date} with a {ld.equipment}? If so I'll shoot the rate con over "
            "for you to sign — that locks it in.",
            instruction=f"You've agreed on ${int(rate)} for the {ld.equipment} load, "
            f"{ld.origin} to {ld.destination}, pickup {ld.pickup_date}. Confirm the rate, "
            f"ask if they can cover that pickup (date/location/equipment), and say you'll "
            "send the rate confirmation to sign to lock it in. Direct and warm.",
            context=f"Agreed rate ${int(rate)}. Pickup {ld.pickup_date} in {city}. "
            f"Equipment {ld.equipment}.",
        )

    def _confirm_booking(self, text: str) -> str:
        lowered = text.lower()
        money = parsing.extract_money(text)

        # Carrier reopens price at the last second -> back to negotiating.
        if money is not None and money != int(self._agreed_rate):
            self.state = CallState.NEGOTIATE
            return self._negotiate(text)

        # Can't cover the pickup -> hand to a rep to rework, don't just book.
        if any(p in lowered for p in _CANNOT_COVER):
            self._repo.log_note(
                self.call_id,
                f"Agreed ${int(self._agreed_rate)} on {self.load.load_id} but carrier "
                f"couldn't confirm the {self.load.pickup_date} pickup — needs a rep to rework.",
            )
            return self._transfer_and_say(reason="pickup_issue")

        # Pickup confirmed -> collect the info a rate con actually needs.
        self.state = CallState.COLLECT_DETAILS
        return self._say(
            "Perfect — what email should I send the rate con to? And who's the driver, "
            "plus the truck or trailer number?",
            instruction="YOU (the rep) are sending the rate confirmation to the carrier. "
            "Ask which email address you should send it to, and for the driver's name and "
            "truck/trailer number. Never imply the carrier sends anything. One short sentence.",
        )

    def _collect_details(self, text: str) -> str:
        email = parsing.extract_email(text)
        phone = parsing.extract_phone(text)
        self._repo.log_note(
            self.call_id,
            f"Booking details for {self.load.load_id} @ ${int(self._agreed_rate)} — "
            f"email: {email or 'not given'}; contact: {phone or 'not given'}; "
            f"raw: \"{text.strip()}\"",
        )
        return self._finalize_booking(details_ok=bool(email or phone))

    def _finalize_booking(self, details_ok: bool = True) -> str:
        rate = int(self._agreed_rate)
        self._repo.book_load(self.load.load_id)
        self._finish(CallOutcome.BOOKED)
        chase = "" if details_ok else " Shoot me the email and driver info when you can. "
        return self._say(
            f"You're locked in on {self.load.load_id} at ${rate} — rate con's headed your "
            f"way, sign it and you're set.{chase} Thanks, drive safe.",
            instruction=f"Confirm they're booked on {self.load.load_id} at ${rate} and "
            "remind them to sign the rate con. "
            + ("" if details_ok else "Also ask them to send over the email and driver "
               "info you still need. ")
            + "Close warmly and briefly.",
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
