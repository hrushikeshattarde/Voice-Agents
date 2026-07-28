"""
CarrierSalesAgent — the call state machine (PRD §3).

Independent of audio I/O: feed it carrier utterances, get back agent speech.

There are no scripted replies here. Each state decides what the turn must ACHIEVE
and which facts and dollar figures may be spoken, and the composer writes the
words after reading what the caller actually said. Every decision — which load,
whether the authority clears, what rate — is made here or in the services; the
model chooses only the wording, and a reply that names money it wasn't given is
rejected and re-prompted.

    GREETING -> IDENTIFY_LOAD -> VERIFY_CARRIER -> ASK_EMPTY
             -> [CHECK_REQUIREMENTS] -> STATE_PRICE -> NEGOTIATE
             -> CONFIRM_BOOKING -> CONFIRM_EMAIL
             -> DONE (booked | transferred | rejected | no_deal)

Nothing about the load is shared — with the caller or with the composer — until
VERIFY_CARRIER clears (ACTIVE authority only) and ASK_EMPTY is answered.
"""

from __future__ import annotations

import enum
import re
import uuid

from lanevoice import formatting, parsing
from lanevoice.db.repository import Repository
from lanevoice.domain.models import (
    CallOutcome,
    Decision,
    OfferParty,
    VerificationAction,
)
from lanevoice.logging_config import get_logger
from lanevoice.services import (
    CarrierVerificationService,
    LoadService,
    NegotiationEngine,
    TransferService,
)
from lanevoice.settings import Settings, get_settings
from lanevoice.voice.composer import TurnComposer

# The one written line in the system. Reached only when the composer cannot
# produce a turn that respects the engine's numbers — see `_cannot_compose`.
_LAST_RESORT = ("Sorry, let me get you over to one of our reps who can pick this "
                "up. One moment.")

logger = get_logger(__name__)

# Failures that will happen again identically on the next attempt: a bad key, a
# model that doesn't exist, a revoked token. Retrying these burns two more round
# trips of dead air on a live call and changes nothing, so we stop at the first.
_UNRETRYABLE = re.compile(
    r"authentication|invalid[\s_-]*api[\s_-]*key|unauthorized|permission|forbidden"
    r"|model[\s_-]*(?:not[\s_-]*found|decommissioned)|does not exist"
    r"|\b40[134]\b",
    re.IGNORECASE,
)

# The agent's persona — handed to the composer as a fact so the whole call sounds
# like one consistent human rep.
REP_NAME = "Alex"

# Non-price levers a rep leans on when a carrier is stuck on their number. ONE
# per turn, and never the same one twice on a call — a pitch delivered verbatim
# two or three times is the most bot-like thing a caller can hear. These are
# spoken to real carriers, so keep them to things the brokerage will actually
# honour — no payment-term promises unless the desk really offers them.
VALUE_LEVERS = (
    "we run this lane every week, so covering this one keeps you first in line "
    "for the next",
    "the shipper loads quick, so you're not going to sit on their dock all day",
    "we turn paperwork around same day, so you're not chasing us to get paid",
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
# The carrier calls their number their best. A rep stops asking at that point and
# either closes it or lets it go — pressing a fourth time just burns the call.
# Deliberately narrow: "final" and "firm" only count when they're attached to the
# RATE, so "what's the final mile", "is the delivery firm" and "let me confirm
# with my driver" don't hand the carrier our money early.
_DECLARES_FINAL_RE = re.compile(
    r"\b(?:that'?s|it'?s) (?:my|the) best\b"
    r"|\bmy best\b"
    r"|\bbest i can (?:do|go)\b"
    r"|\ball i can do\b"
    r"|\b(?:lowest|as low as) i can\b"
    r"|\bbottom line\b"
    r"|\btake it or leave it\b"
    r"|\b(?:can'?t|won'?t|cannot) go (?:any )?lower\b"
    r"|\b(?:no|nothing) (?:lower|less)\b"
    r"|\bfinal (?:offer|number|answer)\b"
    r"|\b(?:that'?s|it'?s) final\b"
    r"|\d\s*,?\s*final\b"                      # "2400 final"
    r"|\b(?:i'?m|we'?re|that'?s|it'?s) firm\b"
    r"|\bfirm at \$?\d{3,}"                    # "firm at 2400"
    r"|\d\s*,?\s*firm\b"                       # "2400 firm"
)


def _declares_final(text: str) -> bool:
    return bool(_DECLARES_FINAL_RE.search(text.lower()))


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


# The engine owns every rate. If the composer slips in a figure we never
# authorized (e.g. splitting the difference on its own — "can you meet me at
# 2200?"), that's a rate leak: the turn is rejected and re-prompted with the
# breach named. Bare numbers count, not just $-prefixed ones — a spoken rate
# doesn't need a dollar sign to move the negotiation.
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


def _speakable(amounts: set[int] | None) -> str:
    """How the turn's money allowance reads to the composer."""
    if amounts is None:
        return "any figure already shown in FACTS"
    if not amounts:
        return ""
    return ", ".join(f"${a}" for a in sorted(amounts))


def _breach(spoken: str, amounts: set[int] | None, must_say: int | None,
            source: str, speakable: str) -> str | None:
    """The fault to send back, or None if the turn is safe to speak.

    Naming the specific breach beats re-rolling the same prompt: the model is far
    likelier to fix "you said a number you weren't given" than to spontaneously
    stop doing it.
    """
    if amounts is not None and _rate_leak(spoken, amounts, source):
        allowed = speakable or "NO dollar amount at all"
        return (f"You named money you were not given. The only money you may say "
                f"this turn is: {allowed}. Say it again with no other figure in it.")
    if must_say is not None and must_say not in _numbers(spoken):
        return (f"You left out ${must_say}, which this turn has to state as YOUR "
                f"number. Say it again and put ${must_say} in it explicitly.")
    return None


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
    ASK_EMPTY = "ask_empty"                     # when/where the truck frees up
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
        composer: TurnComposer,
        settings: Settings | None = None,
    ):
        self._repo = repo
        self._composer = composer
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
        self._levers_used = 0                    # non-price pitches already spent
        self._empty_location: str | None = None  # where their truck frees up
        self._empty_when: str | None = None      # and when
        self._empty_followups = 0                # re-asks for the half we missed
        self._empty_raw: list[str] = []           # exactly what they said, for the note
        self._load_revealed = False               # gate: load facts reach the LLM only after this
        self.transcript: list[tuple[str, str]] = []
        self.outcome: CallOutcome | None = None
        repo.start_call(self.call_id)

    # -- speaking ----------------------------------------------------------- #
    def _dialogue(self) -> str:
        """The whole call so far. The composer needs all of it: a caller's
        question three turns ago is still theirs to have answered."""
        speaker = {"agent": f"You ({REP_NAME})", "carrier": "Caller"}
        return "\n".join(
            f"{speaker.get(who, who)}: {line}" for who, line in self.transcript
        )

    def _standing_facts(self) -> str:
        """Facts that stay true for the rest of the call, so any turn can field a
        question thrown in sideways — "how much does it weigh?", "when's it
        deliver?" — instead of ploughing on with its own agenda.

        The load only appears here once it has actually been revealed. Before the
        empty call the composer must not even know the lane, or it will happily
        volunteer it to a carrier we haven't cleared yet.
        """
        rows = []
        if self.carrier:
            rows.append(f"Caller's company: {self.carrier.legal_name}")
        summary = self._empty_summary()
        if summary:
            rows.append(f"Their truck: {summary}")
        if self._load_revealed and self.load:
            rows.append(f"The load under discussion:\n{self.load.facts()}")
        return "\n".join(rows)

    def _say(self, directive: str, facts: str = "",
             amounts: set[int] | None = None, must_say: int | None = None) -> str:
        """Compose this turn with the LLM and speak it.

        There is no scripted line behind this. `directive` says what the turn has
        to ACHIEVE and `facts` says what may be said; the model writes the words,
        having read what the caller actually just said. That is the whole point —
        the reply is shaped by the call, not chosen from a list of sentences.

        What the model does not get to choose is money. `amounts` is the exact set
        of dollar figures this turn may utter (empty set = none at all; None =
        money isn't in play this turn), and `must_say` is a rate the turn is
        REQUIRED to state, so a reply can't quietly drop our offer and imply we
        took theirs. A breach is re-prompted with the specific fault named; if
        every attempt breaches, the call goes to a person rather than to a script.
        """
        facts = "\n".join(p for p in (facts, self._standing_facts()) if p)
        # The leak check reads the directive and FACTS only — never the dialogue,
        # or the model could recycle an old number as if it were a fresh offer.
        source = f"{directive} {facts}"
        speakable = _speakable(amounts)
        correction = ""

        attempts = max(1, self._settings.llm_attempts)
        why = "no attempt was made"
        for attempt in range(1, attempts + 1):
            try:
                spoken = self._composer.compose(
                    directive=directive,
                    facts=facts,
                    dialogue=self._dialogue(),
                    speakable=speakable,
                    correction=correction,
                )
            except Exception as exc:  # noqa: BLE001 - a flaky API must not drop the call
                # Never silent: an unreachable model and a model that keeps
                # inventing rates both end in a handoff, and whoever reads the log
                # needs to know which one they're looking at.
                why = f"{type(exc).__name__}: {exc}"
                logger.warning("composer failed (attempt %d/%d) in state %s — %s",
                               attempt, attempts, self.state.value, why)
                if _UNRETRYABLE.search(str(exc)) or _UNRETRYABLE.search(
                        type(exc).__name__):
                    logger.error(
                        "composer cannot be reached and retrying will not help. "
                        "Check GROQ_API_KEY and LLM_MODEL in your .env, or set "
                        "USE_LLM=false to drive the flow with the offline stub.")
                    break
                continue
            if not spoken.strip():
                why = "the composer returned nothing"
                correction = "You returned nothing at all. Say your turn out loud."
                continue
            breach = _breach(spoken, amounts, must_say, source, speakable)
            if breach is None:
                self.transcript.append(("agent", spoken))
                return spoken
            why = breach
            logger.warning("rejected composed turn in state %s — %s",
                           self.state.value, breach)
            correction = breach

        return self._cannot_compose(why)

    def _cannot_compose(self, why: str) -> str:
        """We could not produce a turn we're allowed to speak, or the model is down.

        This is the one line in the system that is written rather than composed,
        and it exists because the alternative on a live call is dead air. It does
        not try to carry the conversation on — it hands the call to a person and
        records exactly why, which is the honest outcome when we can't speak safely.
        """
        logger.error("handing call %s to a rep: could not compose a turn in state "
                     "%s (%s)", self.call_id, self.state.value, why)
        self._repo.log_note(
            self.call_id,
            f"Could not compose a compliant turn in state {self.state.value} "
            f"after up to {self._settings.llm_attempts} attempts — handed to a rep. "
            f"Last failure: {why}",
        )
        rep_id = None
        if self.load is not None:
            resolution = self._transfers.resolve(self.load)
            rep_id = resolution.rep.rep_id if resolution.rep else None
        self._finish(CallOutcome.TRANSFERRED, rep_id=rep_id)
        self.transcript.append(("agent", _LAST_RESORT))
        return _LAST_RESORT

    def _log_user(self, text: str) -> None:
        self.transcript.append(("carrier", text))

    def _next_lever(self) -> str:
        """A fresh non-price selling point each time we lean on one, so the same
        pitch never gets repeated at the caller."""
        lever = VALUE_LEVERS[min(self._levers_used, len(VALUE_LEVERS) - 1)]
        self._levers_used += 1
        return lever

    # -- entry points ------------------------------------------------------- #
    def greeting(self) -> str:
        self.state = CallState.IDENTIFY_LOAD
        return self._say(
            "Answer the inbound call. Name the company, give your first name, and ask "
            "what you can help with — that's it. Real desks answer short; do not "
            "deliver a speech, do not list what you do, and do not ask for a load "
            "number yet. One sentence.",
            facts=f"Your name: {REP_NAME}. Brokerage: Circle Logistics.",
            amounts=set(),
        )

    def handle(self, user_text: str) -> str:
        self._log_user(user_text)
        handler = {
            CallState.IDENTIFY_LOAD: self._identify_load,
            CallState.VERIFY_CARRIER: self._verify_carrier,
            CallState.ASK_EMPTY: self._ask_empty,
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
                "You didn't catch a load or reference number in what they just said. Ask "
                "for it — if they described a lane or a posting instead of a number, ask "
                "whether the posting shows a reference number. Do not invent a load, and "
                "do not read them the open list yet.",
                facts="Example of the number format: L1001.",
                amounts=set(),
            )
        # Only load ID NUMBERS are shared before verification — never lanes/details.
        result = self._loads.lookup(load_id)
        open_ids = ", ".join(self._loads.open_load_ids())
        no_details = (
            "You may say these load numbers and NOTHING else about them — no city, "
            f"state, lane, equipment, date, commodity or rate: {open_ids}"
        )
        if not result.found:
            return self._say(
                f"You have no load {load_id} on the board. Tell them so and offer the "
                f"open ones by number, then ask which they want. {no_details}",
                amounts=set(),
            )
        if not result.posted:
            return self._say(
                f"Load {load_id} exists but isn't posted, so you can't book it. Say that "
                f"much, offer the open ones by number, ask which they want. {no_details}",
                amounts=set(),
            )
        if not result.available:
            return self._say(
                f"Load {load_id} is already covered. Tell them, offer the other open ones "
                f"by number, and ask which they want. {no_details}",
                amounts=set(),
            )

        # Valid, posted, open — but nothing about it comes out yet. Read the number
        # back first: the caller is holding a posting and needs to know we're both
        # looking at the same load before they hand over their MC.
        self.load = result.load
        self.state = CallState.VERIFY_CARRIER
        return self._say(
            f"Read the load number {load_id} back to them digit by digit to confirm you "
            f"have the right one, then ask for their MC or USDOT number. Say NOTHING "
            "about the lane, cities, equipment, dates or rate — none of that comes out "
            "until they're verified.",
            facts=f"Load number: {load_id}. Read back as: "
            f"{formatting.spell_digits(load_id)}",
            amounts=set(),
        )

    # -- Step 3: verify carrier -------------------------------------------- #
    def _verify_carrier(self, text: str) -> str:
        _, number = parsing.extract_mc_dot(text)
        if not number:
            return self._say(
                "You didn't catch a number. Say so and ask them to give you their MC or "
                "USDOT number again, slowly.",
                amounts=set(),
            )
        result = self._verifier.verify(number)

        if not result.verified:
            # Authority is not ACTIVE, or insurance isn't on file. Either way this
            # carrier cannot haul for us and there is no rate conversation to have.
            self._repo.log_note(
                self.call_id,
                f"Verification failed on {number} "
                f"({result.carrier.legal_name if result.carrier else 'not found'}): "
                f"{result.reason}, flags {list(result.risk_flags)} — no rate discussed.",
            )
            self._finish(CallOutcome.REJECTED)
            return self._say(
                "You cannot confirm active operating authority and insurance for them, so "
                "you cannot talk about this load or its rate at all. Tell them that "
                "plainly, say it's going to your team to review and someone will follow "
                "up, and close the call politely. Do NOT say which check failed, do not "
                "speculate about why, and do not suggest how they might get around it.",
                amounts=set(),
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
                "Your brokerage isn't set up to work with their company, so you can't book "
                "this one with them. Tell them briefly and politely and close the call. Do "
                "NOT explain why, do not mention any list, and do not offer a workaround.",
                amounts=set(),
            )

        if result.action == VerificationAction.HUMAN_REVIEW:
            self._repo.log_note(
                self.call_id,
                f"Carrier {self.carrier.legal_name} verified but flagged "
                f"{list(result.risk_flags)} — routed to a rep.",
            )
            self._transfer(reason="verification_review")
            return self._say(
                "You're putting them through to a rep to finish getting them set up. Say "
                "so smoothly and keep it moving — nothing accusatory, no detail about what "
                "flagged, and do not discuss the load or a rate.",
                amounts=set(),
            )

        # Approved & verified — but the load STILL doesn't come out yet. A rep's
        # next question is the empty call: where the truck frees up and when. It
        # tells us whether they can even make the pickup, and it's the answer we
        # lean on later when they push on rate.
        self.state = CallState.ASK_EMPTY
        return self._say(
            "You've found them in the system. Check their company name back with them as "
            "a question, then ask where their truck is getting empty and when. Never read "
            "the MC or USDOT digits back. Say NOTHING about the lane, cities, equipment, "
            "dates or rate — that comes after you know where their truck is.",
            amounts=set(),
        )

    # -- The empty call: when and where does the truck free up? ------------- #
    def _ask_empty(self, text: str) -> str:
        """Real desks ask this before quoting anything. Both halves matter, and
        callers routinely give one — so we follow up on the half we missed rather
        than re-asking the whole question."""
        self._empty_raw.append(text.strip())
        self._empty_location = self._empty_location or parsing.extract_empty_location(text)
        self._empty_when = self._empty_when or parsing.extract_empty_when(text)

        if self._empty_followups < 2 and not (self._empty_location and self._empty_when):
            self._empty_followups += 1
            if self._empty_location:
                return self._say(
                    "They told you WHERE the truck empties but not WHEN. Acknowledge the "
                    "place in two or three words and ask when it's going to be empty. Do "
                    "not ask where again. Nothing about the load or the rate yet.",
                    amounts=set(),
                )
            if self._empty_when:
                return self._say(
                    "They told you WHEN the truck empties but not WHERE. Acknowledge the "
                    "timing in two or three words and ask what city and state it's empty "
                    "in. Do not ask when again. Nothing about the load or the rate yet.",
                    amounts=set(),
                )
            return self._say(
                "You caught neither the place nor the timing in that. Say you didn't catch "
                "it and ask again where their truck is getting empty and when. Nothing "
                "about the load or the rate yet.",
                amounts=set(),
            )

        self._repo.log_note(
            self.call_id,
            f"Empty call for {self.load.load_id}: "
            f"where={self._empty_location or 'NOT CAPTURED'}, "
            f"when={self._empty_when or 'NOT CAPTURED'} "
            f"(raw: {' | '.join(self._empty_raw)!r})",
        )
        return self._reveal_load()

    def _empty_summary(self) -> str:
        """What we know about their truck, for the phrasing context."""
        if self._empty_location and self._empty_when:
            return f"Their truck is empty in {self._empty_location} {self._empty_when}."
        if self._empty_location:
            return f"Their truck is empty in {self._empty_location}."
        if self._empty_when:
            return f"Their truck is empty {self._empty_when}."
        return ""

    def _reveal_load(self) -> str:
        """Empty call done — now the load comes out. If it has special
        requirements, read them and confirm BEFORE talking rate."""
        self._load_revealed = True       # from here the composer may quote the load
        if self.load.notes:
            self.state = CallState.CHECK_REQUIREMENTS
            return self._say(
                "Give them the load: what it is, the lane, when it picks up and delivers "
                "with the appointment windows, the commodity and piece count, the "
                "equipment, and the miles. One flowing rundown, the way a rep reads it off "
                "a screen — not a list. THEN read them the special requirements and ask "
                "straight out whether they can do that. Say NOTHING about rate yet; that "
                "comes only once they've said they can meet the requirements.",
                amounts=set(),
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
            max_pulls=self._settings.negotiation_max_pulls,
        )
        self.state = CallState.STATE_PRICE
        opening = int(self.neg.current_offer)
        self._repo.log_offer(self.call_id, 0, OfferParty.AGENT, opening)
        if with_details:
            self._load_revealed = True
            return self._say(
                "Give them the load: what it is, the lane, when it picks up and delivers "
                "with the appointment windows, the commodity and piece count, the "
                "equipment, and the miles. One flowing rundown, the way a rep reads it off "
                f"a screen — not a list. Then put YOUR number on it: you're asking "
                f"${opening} on this one. It is YOUR asking rate, not theirs — \"I've got "
                f"it at ${opening}\", never \"you're at ${opening}\". Finish by asking if "
                "they want the load. They already know they're verified, so don't tell "
                "them again.",
                amounts={opening},
                must_say=opening,
            )
        return self._say(
            f"They already have the load details, so go straight to rate: you're asking "
            f"${opening} on this one — your number, not theirs — and ask whether that "
            "works for them. Short.",
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
                "They've said they can't meet this load's requirements, so you can't book "
                "it with them. Say that without making them feel bad about it, leave the "
                "door open for the next one, and close the call. Two sentences, no rate.",
                amounts=set(),
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
            # No number and no rejection — they said something else entirely
            # (a question, a comment). Answer it, then steer back to their number.
            return self._say(
                "They didn't give you a rate. Deal with whatever they actually just said "
                "first — if it's a question you can answer from FACTS, answer it — then "
                "ask what they need to get on this load. Do not name a figure yourself.",
                amounts=set(),
            )

        self.state = CallState.NEGOTIATE
        return self._apply_negotiation(
            self.neg.evaluate(ask, carrier_final=_declares_final(text)), ask)

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

        # On a HOLD or a PULL nothing new goes on the table — the whole point is
        # that the next concession is theirs, so the LLM must not soften it with a
        # number of its own.
        no_new_number = (
            f"You may name ONLY two numbers: their ${ask_i} and your ${rate}. Do NOT "
            "invent, offer, or hint at any other figure, and do NOT split the "
            "difference — you are not moving on this turn. Never reveal your max."
        )

        # PULL — they came down, and we do NOT pay them for it. Credit the move,
        # restate our number, and put the next one back on them. A carrier who is
        # still coming down can usually come down again; answering every step of
        # theirs with a step of ours is how a desk gives away its margin.
        if result.decision == Decision.PULL:
            if result.pull_number <= 1:
                return self._say(
                    f"They came down to ${ask_i}. Give them one short beat of credit for "
                    f"moving, restate that YOUR number is still ${rate}, and ask how close "
                    f"they can get to ${rate}. You are NOT countering and NOT raising your "
                    f"offer — the next move is theirs, so end on the question. "
                    f"{no_new_number}",
                    facts=f"{lane} You are holding at ${rate}. They just moved to ${ask_i}.",
                    amounts={rate, ask_i},
                    must_say=rate,
                )
            lever = self._next_lever()
            return self._say(
                f"They moved again, to ${ask_i}, still short of your ${rate}. Do NOT "
                f"counter and do NOT raise your offer. Sell the load on this one non-price "
                f"point: {lever}. Then ask where they actually need to be to make it work. "
                f"Firm and friendly, no begging. {no_new_number}",
                facts=f"{lane} You are holding at ${rate}. They are at ${ask_i}. "
                f"Non-price point to use: {lever}",
                amounts={rate, ask_i},
                must_say=rate,
            )

        # HOLD — stay on our number and make THEM come down. No new figure, no
        # splitting the difference: we don't bid against ourselves.
        if result.decision == Decision.HOLD:
            if result.hold_number <= 1 and self.neg.concessions:
                # We've already moved and they haven't. Say so — the imbalance IS
                # the argument, and repeating the discovery question sounds canned.
                opened = int(self.neg.floor)
                return self._say(
                    f"You already moved from ${opened} up to ${rate}; they are still "
                    f"sitting on ${ask_i}. Point the imbalance out — you've moved, they "
                    f"haven't — and ask them to come your way now. Not rude about it. "
                    f"{no_new_number} You may also mention your opening ${opened}.",
                    facts=f"{lane} You opened at ${opened} and are now at ${rate}. They "
                    f"have not moved off ${ask_i}.",
                    amounts={rate, ask_i, opened},
                    must_say=rate,
                )
            if result.hold_number <= 1:
                # A rep's first move on a high ask isn't a counter — it's a question.
                # We took the empty call earlier, so we already know where their truck
                # is: use it instead of asking them the same thing twice.
                if self._empty_location:
                    probe_note = (f"You ALREADY know their truck empties in "
                                  f"{self._empty_location} — do NOT ask where they're "
                                  "coming out of. Refer to it and ask what's driving "
                                  "their number.")
                else:
                    probe_note = ("Ask ONE short question about what's driving their "
                                  "number — where they're coming out of, or whether "
                                  "they're deadheading in.")
                return self._say(
                    f"They asked for ${ask_i}. Say you can't get there and restate that "
                    f"YOUR number is ${rate}. {probe_note} Then ask how close they can get "
                    f"to ${rate}. The target you name is ${rate}, never ${ask_i} — you are "
                    f"asking THEM to come down to you, not offering to come up. Do not "
                    f"call their number too high or above market: you might end up paying "
                    f"it. {no_new_number}",
                    facts=f"{lane} You are holding at ${rate}.",
                    amounts={rate, ask_i},
                    must_say=rate,
                )
            # Second push: they still haven't moved, so sell the load, not the
            # rate. Levers are free; dollars are not.
            lever = self._next_lever()
            return self._say(
                f"They repeated ${ask_i} without moving. Point out you're both where you "
                f"started and that you're still at ${rate}. Instead of raising your number, "
                f"sell the load on this one non-price point: {lever}. Then press them for "
                f"the best number they can actually do. Firm, friendly, no begging. "
                f"{no_new_number}",
                facts=f"{lane} You are holding at ${rate}. They have not moved. "
                f"Non-price point to use: {lever}",
                amounts={rate, ask_i},
                must_say=rate,
            )

        # COUNTER — our ONE closing move. We've held and made them walk; now we
        # spend once, decisively, and ask for the load in the same breath.
        if result.is_split:
            return self._say(
                f"They're at ${ask_i}. Make your one move: you'll come to ${rate}, and ask "
                f"for the load right there and then — say yes and you'll get them covered. "
                f"This is you coming to them, so own it. Confident and near-final without "
                f"saying it's your last offer. Do NOT describe it as splitting anything or "
                f"meeting in the middle. Say no figure other than ${rate} and their "
                f"${ask_i}.",
                facts=f"{lane} Your closing offer is ${rate}. Never reveal your maximum.",
                amounts={rate, ask_i},
                must_say=rate,
            )
        # The only other way money goes on the table is the best-and-final. The
        # engine never produces a plain incremental counter any more — it holds,
        # pulls, closes once, then makes its best number.
        return self._say(
            f"Make your final, best offer: ${rate}. Warm but firm — this is as high as you "
            f"can go on this load — and ask them to take it now. Do NOT call it a cap or a "
            f"maximum and do not reveal any internal number. Say no figure other than "
            f"${rate} and their ${ask_i}.",
            facts=f"{lane} Final best offer ${rate}. Never call it your ceiling or maximum.",
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
        return self._say(
            f"You've agreed on ${int(rate)}. Say the number back so it's on the record, "
            f"then ask whether they can cover that pickup — the day, the place and the "
            f"equipment — and tell them you'll send the rate confirmation over to sign, "
            f"which is what locks it in. Direct and warm.",
            facts=f"Agreed rate: ${int(rate)}",
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
            "They can cover the pickup. Ask which email address to send the rate "
            "confirmation to. YOU are sending it to THEM — never imply they send you "
            "anything. Do NOT invent, guess or suggest an address, and do not read one off "
            "their file: wait for them to say it. Don't ask for a driver name or a truck "
            "number. One short question.",
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
                "You didn't get a usable email address out of that. Say you didn't catch it "
                "and ask again for the best address for the rate confirmation. One short "
                "sentence. Do NOT invent, guess or suggest an address.",
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
        if self._booking_email:
            # Naming the address out loud is the carrier's last chance to catch a
            # wrong one before the con lands in an inbox nobody reads.
            email_note = (f"Tell them the rate confirmation is going to "
                          f"{self._booking_email} — say that address exactly as written, "
                          "do not alter or improve it — and to sign it. ")
            if self._email_is_new:
                email_note += "Mention you've saved that address to their file. "
            facts = f"Rate confirmation goes to: {self._booking_email}"
        else:
            # Nothing to sign yet — don't pretend a con is on its way.
            email_note = ("You still don't have an email for them, so ask them to send one "
                          "over so you can get the rate confirmation out. Do NOT invent an "
                          "address and do NOT say the confirmation is already on its way. ")
            facts = "You have no email address for them."
        return self._say(
            f"Confirm they're booked on this load at ${rate}. {email_note}Close warmly and "
            f"briefly — they're a driver who wants to get moving.",
            facts=facts,
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
                    "Their number is above what you can approve on your own, and nobody "
                    "senior is free this second. Tell them that, and that a rep will call "
                    "them straight back on this load — ask them to sit tight rather than "
                    "taking something else. Keep it hopeful: this is not a no. Name NO "
                    "dollar figure.",
                    amounts=set(),
                )
            return self._say(
                f"Their number is above what YOU can approve on your own, but it is NOT a "
                f"no. Tell them that and that you're putting them through to {who}, who can "
                f"sign off on it. Warm and hopeful. Name NO dollar figure and do not "
                f"mention limits, caps or maximums.",
                facts=f"Rep taking the call: {who}",
                amounts=set(),
            )
        if resolution.rep is None:
            return self._say(
                "No rep is free right now. Tell them you've logged a callback and someone "
                "will get straight back to them. Brief and apologetic without grovelling. "
                "Name NO dollar figure.",
                amounts=set(),
            )
        return self._say(
            f"Tell them you're putting them through to {resolution.rep.name}, who handles "
            f"this load, and to hold a moment. Name NO dollar figure.",
            facts=f"Rep taking the call: {resolution.rep.name}",
            amounts=set(),
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
            "You've come up as far as you can and you're still apart, so this one isn't "
            "happening today. Say so, then leave the door properly open — you run freight "
            "through there regularly, so ask them to call you if their number moves or when "
            "they're back through. Warm, two sentences. Reveal NO numbers.",
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
