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

# Non-price levers a rep leans on when a carrier is stuck on their number. These
# are spoken to real carriers, so keep them to things the brokerage will actually
# honour — no payment-term promises unless the desk really offers them.
VALUE_LEVERS = (
    "we run this lane regularly, so covering this one keeps you first in line "
    "for the next; the shipper loads quick, so you're not sitting on a dock; "
    "and paperwork gets turned around same day"
)

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


# The engine owns every rate. If the phrasing LLM slips in a figure we never
# authorized (e.g. splitting the difference on its own — "can you meet me at
# 2200?"), that's a rate leak: we detect it and speak the deterministic template
# instead. Bare numbers count, not just $-prefixed ones — a spoken rate doesn't
# need a dollar sign to move the negotiation.
_DOLLAR_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _to_ints(raws: list[str]) -> set[int]:
    out = set()
    for raw in raws:
        try:
            out.add(int(float(raw.replace(",", ""))))
        except ValueError:
            continue
    return out


def _dollar_amounts(text: str) -> set[int]:
    return _to_ints(_DOLLAR_RE.findall(text))


def _numbers(text: str) -> set[int]:
    return _to_ints(_NUMBER_RE.findall(text))


def _rate_leak(text: str, allowed: set[int], source: str) -> bool:
    """True if `text` speaks a figure this turn wasn't authorized to speak.

    `allowed` is the set of rates the engine sanctioned for this turn. A dollar
    figure outside it is always a leak. A BARE number is a leak too, unless it
    already appears in what we handed the LLM (`source`: the instruction and the
    facts) — that's how load IDs, pickup dates and weights get through.
    """
    if any(a not in allowed for a in _dollar_amounts(text)):
        return True
    speakable = allowed | _numbers(source)
    return any(n not in speakable for n in _numbers(text))


# The carrier points at our records instead of reciting an address: "just use the
# one you've got". Only trusted when we actually have one on file.
_DEFERS_TO_FILE_RE = re.compile(
    r"(on file|you (?:already )?(?:have|got)|the one you|same (?:one|as )|"
    r"usual (?:one|email)?|whatever you have|your records|as (?:before|last time)|"
    r"last time)"
)


def _defers_to_file(text: str) -> bool:
    return bool(_DEFERS_TO_FILE_RE.search(text.lower()))


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
    CONFIRM_EMAIL = "confirm_email"       # carrier confirms where the rate con goes
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
        self._booking_email: str | None = None   # where the rate con link goes
        self._email_is_new = False               # wasn't on file -> we saved it
        self._email_asks = 0                     # re-asks when we didn't catch one
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
             context: str = "", amounts: set[int] | None = None,
             must_say: int | None = None) -> str:
        """`amounts` = the only dollar figures this turn is allowed to speak.
        Pass an empty set for turns that must name no rate at all; pass None to
        skip the check on turns where money never comes up.

        `must_say` = a rate the turn MUST put on the table. Without it the LLM can
        drop our number and imply we took theirs ("you came down to $2400, I'll
        meet you there") — so if it's missing we speak the template instead."""
        text = fallback
        if self._phraser is not None and instruction is not None:
            convo = self._recent_dialogue()
            full_context = f"Recent conversation:\n{convo}\n\n{context}".strip() \
                if convo else context
            try:
                phrased = self._phraser.phrase(instruction, full_context)
            except Exception:  # noqa: BLE001 - phrasing must never break a call
                phrased = ""
            # Leak check runs against the instruction + FACTS only — never the
            # recent dialogue, or the LLM could recycle an old number as a new offer.
            if (
                phrased
                and (
                    amounts is None
                    or not _rate_leak(phrased, amounts, f"{instruction} {context}")
                )
                and (must_say is None or must_say in _numbers(phrased))
            ):
                text = phrased
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
            CallState.CONFIRM_EMAIL: self._confirm_email,
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
        # Only load ID NUMBERS are shared before verification — never lanes/details.
        result = self._loads.lookup(load_id)
        open_ids = ", ".join(self._loads.open_load_ids())
        _no_details = ("List ONLY these load ID numbers and nothing else — do NOT "
                       f"mention any city, state, lane, equipment, rate, or details: {open_ids}")
        if not result.found:
            return self._say(
                f"I don't see load {load_id}. The open ones right now are {open_ids}. "
                "Which would you like?",
                instruction=f"Say you couldn't find load {load_id}, then offer the open "
                f"loads by ID only. {_no_details}",
            )
        if not result.posted:
            return self._say(
                f"Load {load_id} isn't posted right now, so I can't book that one. "
                f"Open ones are {open_ids}. Which would you like?",
                instruction=f"Say load {load_id} isn't posted/available, then offer the "
                f"open loads by ID only. {_no_details}",
            )
        if not result.available:
            return self._say(
                f"Load {load_id} is already covered. Other open ones are {open_ids}.",
                instruction=f"Say load {load_id} is already covered, then offer the open "
                f"loads by ID only. {_no_details}",
            )

        # Valid, posted, open — but DON'T reveal any details yet. Verify first.
        self.load = result.load
        self.state = CallState.VERIFY_CARRIER
        return self._say(
            f"Got it, {result.load.load_id}. Before I go over it, can I grab your MC or "
            "USDOT number?",
            instruction=f"Acknowledge you've got load {result.load.load_id} (just the ID). "
            "Do NOT mention the lane, cities, equipment, pickup, or any details yet — you "
            "share those only after verifying. Ask for their MC or USDOT number.",
            context=f"Load {result.load.load_id}. Do NOT reveal any load details yet.",
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

        # Approved & verified — NOW it's safe to share the load. If the load has
        # special requirements, present it + read them out and confirm BEFORE rate.
        ld = self.load
        details = (f"{ld.load_id}, {ld.origin.split(',')[0]} to {ld.destination.split(',')[0]}, "
                   f"{ld.equipment}, picking up {ld.pickup_date}")
        if ld.notes:
            self.state = CallState.CHECK_REQUIREMENTS
            return self._say(
                f"You're verified, {self.carrier.legal_name}. Here's the load — {details}. "
                f"One thing though — {ld.notes} Can you handle that?",
                instruction=f"Tell the carrier {self.carrier.legal_name} they're verified "
                "(by company name, never the MC number). NOW share the load details: "
                f"{details}. Then read the special requirements verbatim: \"{ld.notes}\" "
                "and ask if they can do it.",
                context=f"Carrier {self.carrier.legal_name}. Load {details}. "
                f"Requirements: {ld.notes}",
                amounts=set(),   # rate comes only after they confirm the requirements
            )
        return self._present_offer(with_details=True)

    def _present_offer(self, with_details: bool = False) -> str:
        """Set up negotiation and make the opening offer. When `with_details`,
        also reveal the load (only reached after verification + approval)."""
        self.neg = NegotiationEngine(
            self.load,
            max_rounds=self._settings.max_negotiation_rounds,
            buffer=self._settings.negotiation_buffer,
            reciprocity=self._settings.negotiation_reciprocity,
            discretion_rate=self._settings.negotiation_discretion_rate,
            settle_gap_rate=self._settings.negotiation_settle_gap_rate,
            split_gap_rate=self._settings.negotiation_split_gap_rate,
            stonewall_final_rate=self._settings.negotiation_stonewall_final_rate,
            max_holds=self._settings.negotiation_max_holds,
        )
        self.state = CallState.STATE_PRICE
        opening = int(self.neg.current_offer)
        self._repo.log_offer(self.call_id, 0, OfferParty.AGENT, opening)
        ld = self.load
        details = (f"{ld.load_id}, {ld.origin.split(',')[0]} to {ld.destination.split(',')[0]}, "
                   f"{ld.equipment}, picking up {ld.pickup_date}")
        if with_details:
            return self._say(
                f"You're verified, {self.carrier.legal_name}. Here's the load — {details}. "
                f"I've got it at ${opening}. That work?",
                instruction=f"Tell the carrier {self.carrier.legal_name} they're verified "
                f"(company name, not the MC number), share the load: {details}, then float "
                f"YOUR opening offer — say you've got it at ${opening} (your number, not "
                f"theirs: \"I've got it at ${opening}\", never \"you're at ${opening}\") — "
                f"and ask if it works. Name no dollar figure other than ${opening}.",
                context=f"Carrier {self.carrier.legal_name}. Load {details}. Opening ${opening}.",
                amounts={opening},
                must_say=opening,
            )
        return self._say(
            f"Great. On rate, I've got this one at ${opening}. Can you work with that?",
            instruction=f"Float YOUR opening offer — say you've got it at ${opening} (your "
            f"number, not theirs) — and ask if it works. Brief; the load details were "
            f"already given. Name no dollar figure other than ${opening}.",
            context=f"Opening offer ${opening}. Never read back the MC number.",
            amounts={opening},
            must_say=opening,
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
                instruction=f"Ask the carrier, naturally, what rate they need on load "
                f"{self.load.load_id}. Do NOT name any dollar figure yourself.",
                amounts=set(),
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

        if result.decision == Decision.ESCALATE:
            return self._escalate(ask, result)

        ask_i = int(ask)
        rate = int(result.rate)
        lane = (f"Load {self.load.load_id}, {self.load.origin.split(',')[0]} to "
                f"{self.load.destination.split(',')[0]}, {self.load.equipment}.")
        self._repo.log_offer(self.call_id, self.neg.round, OfferParty.AGENT, rate)

        # HOLD — stay on our number and make THEM come down. No new figure, no
        # splitting the difference: we don't bid against ourselves.
        if result.decision == Decision.HOLD:
            no_new_number = (
                f"You may name ONLY two numbers: their ${ask_i} and your ${rate}. Do NOT "
                "invent, offer, or hint at any other figure, and do NOT split the "
                "difference — you are not moving on this turn. Never reveal your max."
            )
            if result.hold_number <= 1 and self.neg.concessions:
                # We've already moved and they haven't. Say so — the imbalance IS
                # the argument, and repeating the discovery question sounds canned.
                opened = int(self.neg.floor)
                return self._say(
                    f"Hang on — I came up from ${opened} to ${rate} and you're still at "
                    f"${ask_i}. I need you to come my way now. What's it take?",
                    instruction=f"You already moved from ${opened} up to ${rate}; the "
                    f"carrier is still sitting on ${ask_i}. Point that out — you've moved, "
                    "they haven't — and ask them to come to you now. Don't be rude about "
                    f"it. {no_new_number} You may also mention your opening ${opened}.",
                    context=f"{lane} You opened at ${opened}, you're now at ${rate}, they "
                    f"haven't moved off ${ask_i}. {no_new_number}",
                    amounts={rate, ask_i, opened},
                    must_say=rate,
                )
            if result.hold_number <= 1:
                # A rep's first move on a high ask isn't a counter — it's a question.
                # Find out what's driving their number before spending a dollar.
                return self._say(
                    f"Yeah, ${ask_i}'s a reach for me on this one — I'm at ${rate}. "
                    "What's got you up there, are you deadheading in? Tell me how close "
                    f"you can get to ${rate}.",
                    instruction=f"The carrier asked for ${ask_i}. Don't call their number "
                    "too-high or 'above market' (you don't want to bluff a number you "
                    f"might pay). Say you can't get to ${ask_i}, restate that YOUR number "
                    f"is ${rate}, ask ONE short question about what's driving their number "
                    "(where they're coming out of / whether they're deadheading in), and "
                    f"ask how close they can get to ${rate}. The target you name must be "
                    f"${rate}, never ${ask_i} — you are asking THEM to come down to you, "
                    f"not offering to come up. {no_new_number}",
                    context=f"{lane} You're holding at ${rate}. {no_new_number}",
                    amounts={rate, ask_i},
                    must_say=rate,
                )
            # Second push: they still haven't moved, so sell the load, not the
            # rate. Levers are free; dollars are not.
            return self._say(
                f"You're still at ${ask_i} and I'm still at ${rate} — I need something "
                f"from you here. Look, {VALUE_LEVERS}. What's the best you can actually "
                "do on this one?",
                instruction=f"The carrier repeated ${ask_i} without moving. Point out "
                f"you're both where you started and you're still at ${rate}. Instead of "
                "raising your number, sell the load on ONE of these non-price points: "
                f"{VALUE_LEVERS}. Then press them for the best number they can actually "
                f"do. Firm, friendly, no begging. {no_new_number}",
                context=f"{lane} You're holding at ${rate}; they haven't moved. "
                f"Value to sell: {VALUE_LEVERS}. {no_new_number}",
                amounts={rate, ask_i},
                must_say=rate,
            )

        # COUNTER — they moved, so we move: a fraction of what they just gave.
        if result.is_split:
            # The classic close: stop trading nickels, cut it down the middle
            # and ask for the load on the spot.
            return self._say(
                f"Alright, we're close — let's not go back and forth over it. Meet me at "
                f"${rate} and I'll book you right now.",
                instruction=f"You and the carrier are close ({ask_i} vs your new ${rate}). "
                f"Offer to meet in the middle at ${rate} and ask for the load RIGHT NOW — "
                "'do that and I'll get you covered on this one'. Confident and final-ish "
                f"without saying it's your last offer. Name no dollar figure other than "
                f"${rate} and their ${ask_i}.",
                context=f"{lane} Your split-the-difference offer is ${rate}. Never reveal "
                "your max.",
                amounts={rate, ask_i},
                must_say=rate,
            )
        if result.is_final:
            return self._say(
                f"Alright — tell you what, I'll stretch to ${rate}. That's honestly the "
                "best I can do on this one. Say yes and it's yours.",
                instruction=f"Make your FINAL, best offer of ${rate}. Convey warmly but "
                "firmly that this is as high as you can go on this load, and ask them to "
                "take it right now. Do NOT say it's a hard cap or reveal any internal "
                f"number. Name no dollar figure other than ${rate} and their ${ask_i}.",
                context=f"{lane} Final best offer ${rate}. Never call it your ceiling/max.",
                amounts={rate, ask_i},
                must_say=rate,
            )
        return self._say(
            f"Okay, you moved so I'll move — I can do ${rate}. That get us there?",
            instruction=f"They came down, so reciprocate with a SMALLER step: your new "
            f"offer is ${rate}. Credit them for moving, state ${rate} out loud, and ask if "
            f"that gets it done. You are NOT taking their ${ask_i}, so never say you'll "
            "'meet them there' or agree to their number. Vary your wording. Name no dollar "
            f"figure other than ${rate} and their ${ask_i}.",
            context=f"{lane} Your improved offer is ${rate}. Never reveal your max.",
            amounts={rate, ask_i},
            must_say=rate,
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
            amounts={int(rate)},
            must_say=int(rate),
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

        # Pickup confirmed -> ask THEM where the rate con goes. We don't read our
        # file at them and call that a confirmation; whatever they say is checked
        # against the record afterwards.
        self.state = CallState.CONFIRM_EMAIL
        return self._say(
            "Perfect — what email should I send the rate con to?",
            instruction="YOU (the rep) are sending the rate confirmation link to the "
            "carrier. Ask which email address to send it to. Never imply the carrier sends "
            "anything. Do NOT invent, guess or suggest an email address — wait for theirs. "
            "Do NOT ask for a driver name, truck or trailer number. One short sentence.",
            amounts=set(),
        )

    def _confirm_email(self, text: str) -> str:
        """The carrier gives the address; we check it against their file.

        Known address -> confirm it back. New one -> confirm it back and append it
        to the carrier record, so the file grows instead of going stale.
        """
        spoken = parsing.extract_email(text)
        on_file = self.carrier.contact_emails

        if spoken:
            known = self._repo.email_on_file(self.carrier.usdot_number, spoken)
            if not known:
                self._repo.add_carrier_email(self.carrier.usdot_number, spoken)
            return self._book_rate_con(
                spoken.lower(),
                "matched carrier file" if known else "new address — added to carrier file",
                text,
                is_new=not known,
            )

        # "Just use the one you've got" — they're pointing at the file rather than
        # reciting it, so read the most recent one back as we send it.
        if on_file and _defers_to_file(text):
            return self._book_rate_con(
                on_file[-1], "carrier deferred to the address on file", text)

        if self._email_asks < 1:
            self._email_asks += 1
            return self._say(
                "Sorry, I didn't catch that — what's the best email for the rate con?",
                instruction="You didn't get a usable email address. Say you didn't catch "
                "it and ask again for the best address to send the rate confirmation to. "
                "One short sentence. Do NOT invent or guess an address.",
                amounts=set(),
            )

        # Asked twice, still nothing usable. Book the load; the con waits.
        return self._book_rate_con(None, "no address given on the call", text)

    def _book_rate_con(self, email: str | None, source: str, text: str,
                       is_new: bool = False) -> str:
        self._booking_email = email
        self._email_is_new = is_new
        self._repo.log_note(
            self.call_id,
            f"Booking for {self.load.load_id} @ ${int(self._agreed_rate)} — rate con to: "
            f"{email or 'NOT CAPTURED — needs follow-up before sending'} ({source}); "
            f"addresses now on file for {self.carrier.legal_name}: "
            f"{', '.join(self._repo.carrier_emails(self.carrier.usdot_number)) or 'none'}; "
            f"raw: \"{text.strip()}\"",
        )
        return self._finalize_booking(details_ok=bool(email))

    def _finalize_booking(self, details_ok: bool = True) -> str:
        rate = int(self._agreed_rate)
        self._repo.book_load(self.load.load_id)
        self._finish(CallOutcome.BOOKED)
        # Name the address the link is going to — that read-back is the carrier's
        # last chance to catch a wrong one before the con lands in a dead inbox.
        if self._booking_email:
            added = " I've added it to your file." if self._email_is_new else ""
            closing = (f" I'm sending the rate con link to {self._booking_email} now."
                       f"{added} Sign it and you're set.")
            email_note = (f"Tell them the rate confirmation link is going to "
                          f"{self._booking_email} — say that address exactly, do not "
                          "alter or invent one. ")
            if self._email_is_new:
                email_note += "Mention you've saved that address to their file. "
        else:
            # Nothing to sign yet — don't pretend a con is on its way.
            closing = (" Text me an email when you get a second and I'll fire the rate "
                       "con over to sign.")
            email_note = ("You still don't have their email, so ask them to send it over "
                          "so you can get the rate con out. Do NOT invent an address, and "
                          "do NOT say the rate confirmation is already on its way. ")
        return self._say(
            f"You're locked in on {self.load.load_id} at ${rate}.{closing} "
            "Thanks, drive safe.",
            instruction=f"Confirm they're booked on {self.load.load_id} at ${rate} and "
            f"remind them to sign the rate con. {email_note}Close warmly and briefly.",
            context=f"Rate con link goes to: {self._booking_email or 'unknown'}.",
            amounts={rate},
            must_say=rate,
        )

    # -- Step 6b: transfer -------------------------------------------------- #
    def _transfer(self, reason: str) -> object:
        resolution = self._transfers.resolve(self.load)
        self._finish(CallOutcome.TRANSFERRED, rep_id=resolution.rep.rep_id
                     if resolution.rep else None)
        return resolution

    def _transfer_and_say(self, reason: str) -> str:
        resolution = self._transfer(reason)
        if reason == "above_agent_authority":
            # Don't hand the carrier a "no" — hand them to someone who can say yes.
            who = resolution.rep.name if resolution.rep else None
            if who is None:
                return self._say(
                    "That number's above what I can approve on my own. My guys are all on "
                    "calls, so let me get one of them to call you right back on it — "
                    "don't take another load just yet.",
                    instruction="Tell the carrier their number is above what you can "
                    "approve yourself, that no one's free this second, and that a rep "
                    "will call them right back about this load. Keep it hopeful — the "
                    "deal is not dead. Do NOT name any dollar figure.",
                    amounts=set(),
                )
            return self._say(
                f"That's above what I can approve on my own, but it's not a no — let me "
                f"get you {who}, who can sign off on it. Hold with me one second.",
                instruction=f"Tell the carrier their number is above what YOU can approve "
                f"on your own, that it is NOT a no, and that you're putting them through "
                f"to {who} who can approve it. Keep it warm and hopeful. Do NOT name any "
                "dollar figure and do NOT mention limits, caps or maximums.",
                amounts=set(),
            )
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

    # -- Above the agent's own authority -> hand it to a human -------------- #
    def _escalate(self, ask: float, result) -> str:
        """Their number is inside Max Buy but above what the agent spends on its
        own. A rep in this spot doesn't walk and doesn't cave — they go ask."""
        offers = ", ".join(f"${int(o)}" for o in self.neg.offers_made) or "n/a"
        self._repo.log_note(
            self.call_id,
            f"ESCALATION on {self.load.load_id} ({self.load.origin} -> "
            f"{self.load.destination}). Carrier {self.carrier.legal_name} "
            f"(USDOT {self.carrier.usdot_number}) is firm at ${int(ask)}. We opened at "
            f"${int(self.neg.floor)} and went to ${int(result.final_offer)} "
            f"(offers: {offers}). ${int(ask)} is WITHIN Max Buy "
            f"${int(self.neg.ceiling)} but above the agent's ${int(self.neg.max_offer)} "
            "authority — a rep can still close this. Warm handoff.",
        )
        return self._transfer_and_say(reason="above_agent_authority")

    # -- No deal: walked up, still apart -> note + decline + end ------------ #
    def _no_deal(self, ask: float, result) -> str:
        offers = ", ".join(f"${int(o)}" for o in self.neg.offers_made) or "n/a"
        self._repo.log_note(
            self.call_id,
            f"NO DEAL on {self.load.load_id} ({self.load.origin} -> "
            f"{self.load.destination}). Carrier {self.carrier.legal_name} "
            f"(USDOT {self.carrier.usdot_number}) held at ${int(ask)}, which is ABOVE "
            f"Max Buy ${int(self.neg.ceiling)} — not workable at any authority level. "
            f"We opened at ${int(self.neg.floor)} and went to "
            f"${int(result.final_offer)} (offers: {offers}) over {self.neg.round} "
            "rounds. Call ended by agent.",
        )
        self._finish(CallOutcome.NO_DEAL)
        return self._say(
            "I've come up as far as I can go on this one and we're still apart, so I "
            "can't make it work today. Keep my number though — if your situation changes "
            "or you're back through here, call me and I'll get you on something. "
            "Drive safe.",
            instruction="Politely say you've come up as far as you can and you're still "
            "apart, so you can't make this one work today. Then leave the door open: ask "
            "them to call you back if their number changes or when they're back in the "
            "area, since you have freight regularly. Do NOT reveal any numbers. Warm, "
            "2 sentences.",
            amounts=set(),
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
