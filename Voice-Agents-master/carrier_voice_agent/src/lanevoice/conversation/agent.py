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

Three gates, in this order, and each one is a hard stop:

  IDENTIFY_LOAD   the load number has to be on the board — posted, open, and
                  carrying a published rate to anchor at.
  VERIFY_CARRIER  the MC/USDOT has to be in the system as ACTIVE. INACTIVE and
                  SUSPENDED are told they don't meet the requirements to work
                  with us; a status we cannot READ goes to a human instead.
  CONFIRM_EMAIL   the address they give has to already be on their account, and
                  the booking has to land in the system of record, before anyone
                  hears the word "booked".

Nothing about the load is shared — with the caller or with the composer — until
VERIFY_CARRIER clears and ASK_EMPTY is answered. Where the loads and carriers
come from (the Transport Pro API or the offline seed data) is the repository's
business, not this file's.
"""

from __future__ import annotations

import enum
import re
import uuid

from lanevoice import formatting, geo, parsing
from lanevoice.db.repository import Repository
from lanevoice.domain.errors import SourceUnavailable
from lanevoice.domain.models import (
    CallOutcome,
    Decision,
    LoadStatus,
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

# How many times we'll ask for an MC/USDOT number before handing the call to a
# person. Spoken digits over phone audio fail often enough that a bot which just
# keeps asking gets hung up on.
_MAX_MC_ASKS = 3
# The requirements gate needs a real YES, not merely the absence of a no. A caller
# who says "Hello." has agreed to nothing — and on a live call that is exactly what
# happened, because the previous turn had been cut off mid-sentence and they thought
# the line had dropped. It was recorded as them confirming a food-grade trailer,
# swing doors and an under-ten-year-old unit.
_REQUIREMENT_YES = (
    "yes", "yeah", "yep", "yup", "sure", "of course", "absolutely", "definitely",
    "correct", "affirmative", "can do", "we can", "i can", "no problem",
    "no worries", "that's fine", "thats fine", "that works", "ok", "okay",
    "got it", "understood", "all good", "we're good", "were good", "fine",
    "sounds good", "not a problem", "10-4", "ten four", "copy",
)
# How many times the requirements are put to a caller who answers neither yes nor
# no before the call goes to a rep. Two, because the usual cause is that they
# didn't hear it, and a rep can read it to them.
_MAX_REQUIREMENT_ASKS = 2
# How many load numbers that don't work before the call is wrapped up. The agent
# never reads out a list of alternatives, so without a cap a caller working from a
# stale posting — or a mangled transcript — could keep the line open forever.
# Three, because speech-to-text mishears digits often enough that one miss is
# usually not the caller's fault.
_MAX_LOAD_ASKS = 3

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

# What a load pitch actually has to contain, shared by the two turns that give one
# out (with the rate, and without).
#
# The list used to be everything on the record — lane, both appointment windows,
# commodity, piece count, equipment, weight, miles — and it read exactly like a
# screen being dictated. Measured on a live call: 27 seconds of speech for the
# rundown and 33 for the requirements right behind it, a solid minute before the
# carrier was asked anything. A rep does not do that; a rep gives the lane, the
# days and the equipment, and answers the rest as it comes up.
#
# Nothing is LOST by cutting it. Every field stays in FACTS and the composer is
# told to answer from FACTS, so a carrier who asks the piece count or the
# delivery window gets it — on the turn they actually want it, instead of thirty
# seconds of detail they have to hold in their head to find the one they wanted.
_PITCH_ESSENTIALS = (
    "COVER, and nothing else: the lane (both cities), the pickup day and the "
    "delivery day, the equipment, and the weight with what it is. Add the miles "
    "only if it fits without a fourth sentence.\n"
    "LEAVE OUT unless they ask: piece count, dimensions, temperature, the "
    "appointment windows, and anything about how check-in works. It is all in "
    "FACTS and you answer it gladly IF ASKED — reciting it unprompted is what "
    "makes a rundown impossible to follow on a phone.\n"
    "If FACTS gives a deadhead to the pickup, work it in the way a rep would — "
    "'it's about ninety miles from you' — and ONLY as the approximation it is; "
    "never state it as an exact figure and never invent one.\n"
)

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
    # "that's my last" / "last I can do" — the same declaration as "my best", and
    # just as common. Missing it cost a live call: the carrier said "2500, that's
    # my last", the engine read it as an ordinary repeat and kept pushing.
    r"|\b(?:that'?s|it'?s) (?:my|the) last\b"
    r"|\bmy last (?:offer|number)?\b"
    r"|\blast i can (?:do|go)\b"
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


# Confirming or denying an identity we read back ("is this Roadrunner Freight?").
# Narrow and word-boundary matched: these decide which carrier we book.
_AFFIRMS_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|correct|right|sure|speaking|affirmative|uh[- ]huh|"
    r"that'?s (?:us|right|me|correct)|we are|i am|it is|you got it)\b"
)
_DENIES_RE = re.compile(
    r"\b(?:no|nope|nah|not us|not me|wrong|different|another|isn'?t|ain'?t)\b"
)


# A caller bowing out at the load-lookup step — "no, that's okay", "no thanks",
# "I'm good, bye". Deliberately narrow: the phrase has to BE the whole turn, so
# "that's okay, but can you check 2520571" (carries a number, never reaches this
# test) and a bare mid-thought "okay" both stay in the conversation. Observed
# live: told a load wasn't posted, the caller said "And that's okay." and the
# agent asked for another number — the very thing they had just declined.
_CALLER_DONE_RE = re.compile(
    r"^[\s,.!]*(?:and\s+|no[,\s]+|nah[,\s]+|nope[,\s]+)?"
    r"(?:no|nope|nah|that'?s?\s+(?:okay|ok|alright|all\s+right|fine|all|it)|"
    r"i'?m\s+(?:good|all\s+set|fine)|we'?re\s+(?:good|all\s+set)|"
    r"no\s+thanks?|no\s+thank\s+you|nothing(?:\s+else)?|never\s?mind|"
    r"all\s+set|forget\s+it|goodbye|bye|have\s+a\s+good\s+(?:one|day)|take\s+care)"
    r"[\s,.!]*(?:thanks?|thank\s+you|though|man|buddy|sir|bye|goodbye)?[\s,.!]*$",
    re.IGNORECASE,
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


# "8:30" spoken is "eight thirty", which the word parser reads as 830. Emitting it
# from both the reply and the source keeps an appointment time from reading as an
# unauthorised figure.
_CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _numbers(text: str) -> set[int]:
    """Every number the text states, in digits OR in words.

    Words matter because the agent's own replies use them: "I'm at twenty-four
    fifty on this one" is how a rep says a rate, and a digit-only scan reads that
    sentence as containing no numbers at all. Both halves of the money guardrail
    were wrong as a result — see `parsing.spoken_numbers`.

    Applied identically to the reply and to the directive/facts it came from, so a
    figure mentioned in either form is recognised in either form.
    """
    # Fold "26 hundred" into "2600" BEFORE scanning, or the digit pass reports a
    # bare 26 alongside the word pass's 2600 and the turn is rejected over a
    # fragment of a figure it was authorised to say. Measured: this is the whole
    # difference between Haiku 4.5 passing the guardrail half the time and
    # passing it every time (`tools/measure_latency.py`).
    folded = parsing.fold_mixed_numbers(text)
    found = _to_ints(_NUMBER_RE.findall(folded))
    found |= parsing.spoken_numbers(folded)
    found |= {int(h) * 100 + int(m) for h, m in _CLOCK_RE.findall(folded)}
    return found


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


def _confirms_requirements(text: str) -> bool:
    """True only if they actually SAID they can meet them.

    The gate this backs used to accept anything that wasn't a refusal, which made
    silence, confusion and "Hello." all read as consent. A driver who agrees to
    food-grade-and-swing-doors without having agreed to anything is a driver
    turned away at the dock.
    """
    low = " " + text.lower().strip() + " "
    return any(word in low for word in _REQUIREMENT_YES)


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
        self._booking_email: str | None = None   # where the booking link goes
        # The issued link, when one was produced. Never spoken — a URL cannot be
        # read down a phone — but it belongs in the call record so a rep can see
        # what the carrier was actually sent.
        self._booking_link: str | None = None
        self._email_asks = 0                     # re-asks when we didn't catch one
        self._mc_asks = 0                        # re-asks for an unheard MC/USDOT
        self._load_asks = 0                      # load numbers that didn't work out
        self._requirements_read = False          # have the board notes been spoken?
        self._requirement_asks = 0               # asks with no yes and no no
        self._mc_digits = ""                     # digits heard so far, across turns
        self._mc_narrowed = None                 # partial that matched one carrier
        self._levers_used = 0                    # non-price pitches already spent
        self._empty_location: str | None = None  # where their truck frees up
        self._empty_when: str | None = None      # and when
        self._empty_followups = 0                # re-asks for the half we missed
        self._empty_raw: list[str] = []           # exactly what they said, for the note
        # (location, load_id) -> spoken deadhead. See `_deadhead_phrase`.
        self._deadhead_cache: tuple[object, str | None] = (None, None)
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
        # Only once the load is out: before that the composer must not know the
        # lane, and a deadhead figure would give away where the pickup is.
        if self._load_revealed and (deadhead := self._deadhead_phrase()):
            rows.append(f"Deadhead to the pickup: {deadhead} (an ESTIMATE — say it "
                        "as approximate, never as an exact figure)")
        if self._load_revealed and self.load:
            rows.append(f"The load under discussion:\n{self.load.facts()}")
        return "\n".join(rows)

    def _deadhead_phrase(self) -> str | None:
        """Roughly how far their truck is from the pickup, or None.

        None for every uncertainty — a caller who named only a state, a town too
        small for the bundled table, a pickup whose record carries no coordinates.
        In each case the agent says nothing about distance, which is the honest
        answer: a confidently wrong deadhead is worse than no deadhead, because a
        driver plans around it.

        Cached per call. The inputs cannot change once the truck's location is
        captured and the load is fixed, and the fuzzy match is not free.
        """
        if self.load is None or not self._empty_location:
            return None
        key = (self._empty_location, self.load.load_id)
        if self._deadhead_cache[0] == key:
            return self._deadhead_cache[1]

        phrase = geo.deadhead_phrase(
            self._empty_location, self.load.origin_lat, self.load.origin_lon,
            road_factor=self._settings.deadhead_road_factor)
        self._deadhead_cache = (key, phrase)
        if phrase:
            logger.info("Deadhead for %s from %r to load %s: %s",
                        self.carrier.legal_name if self.carrier else "caller",
                        self._empty_location, self.load.load_id, phrase)
        return phrase

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
                        "Check %s and LLM_MODEL in your .env, or set "
                        "USE_LLM=false to drive the flow with the offline stub.",
                        self._settings.llm_key_name)
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
        # A call can break before it has a load — `resolve` takes that.
        resolution = self._transfers.resolve(self.load)
        rep_id = resolution.rep.rep_id if resolution.rep else None
        self._finish(CallOutcome.TRANSFERRED, rep_id=rep_id)
        self.transcript.append(("agent", _LAST_RESORT))
        return _LAST_RESORT

    def _log_user(self, text: str) -> None:
        self.transcript.append(("carrier", text))

    def _note(self, note: str) -> None:
        """Record a note against the call, and against the load in the TMS.

        Everything the agent decides that a human might have to answer for goes
        through here. The local copy is the audit trail; the copy on the load is
        what a rep actually sees when they open it, which is where they will be
        looking when the carrier rings back. The TMS write is best-effort — the
        repository swallows and logs its own failures — so a flaky API costs us a
        note, never the call.
        """
        self._repo.log_note(self.call_id, note)
        if self.load is not None and self._settings.post_load_notes:
            self._repo.post_load_note(
                self.load.load_id, f"[Voice AI call {self.call_id}] {note}")

    def _backend_failure(self, exc: Exception) -> str:
        """The system of record didn't answer. Hand over; don't guess.

        Every question left at this point — is that load still open, is this
        carrier active, is that address theirs — is one where a confident wrong
        answer is worse for the carrier than a handoff. So we don't invent one.
        """
        logger.error("handing call %s to a rep: the system of record is "
                     "unavailable in state %s — %s",
                     self.call_id, self.state.value, exc)
        self._repo.log_note(
            self.call_id,
            f"System of record unavailable in state {self.state.value} "
            f"({type(exc).__name__}: {exc}) — handed to a rep without answering "
            "the caller's question.",
        )
        return self._transfer_and_say(reason="source_unavailable")

    def _next_lever(self) -> str:
        """A fresh non-price selling point each time we lean on one, so the same
        pitch never gets repeated at the caller."""
        lever = VALUE_LEVERS[min(self._levers_used, len(VALUE_LEVERS) - 1)]
        self._levers_used += 1
        return lever

    def _sync_transcript(self) -> None:
        """Persist the transcript-so-far after each turn.

        Two reasons. A live view (the dashboard's Runs page) can read the call
        while it is still happening; and a worker crash mid-call loses nothing.
        It also picks up the agent's FINAL line on concluded calls: `_finish`
        writes the record before the goodbye is composed, so without this last
        sync the stored transcript would always be one line short.

        Best-effort by design — a failed bookkeeping write must never end a
        call that is otherwise going fine.
        """
        try:
            self._repo.update_transcript(self.call_id, self.transcript)
        except Exception:  # noqa: BLE001 - never let bookkeeping end a call
            logger.warning("could not sync live transcript for %s",
                           self.call_id, exc_info=True)

    # -- entry points ------------------------------------------------------- #
    def greeting(self) -> str:
        self.state = CallState.IDENTIFY_LOAD
        spoken = self._say(
            "Answer the inbound call. Name the company, give your first name, and ask "
            "what you can help with — that's it. Real desks answer short; do not "
            "deliver a speech, do not list what you do, and do not ask for a load "
            "number yet. One sentence.",
            facts=f"Your name: {REP_NAME}. Brokerage: Circle Logistics.",
            amounts=set(),
        )
        self._sync_transcript()
        return spoken

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
        try:
            return handler(user_text)
        except SourceUnavailable as exc:
            # The board, the carrier file or the contact list is unreachable. Not
            # recoverable by asking the caller anything, so stop asking.
            return self._backend_failure(exc)
        finally:
            # Every exit path — including a handler that just finished the call.
            self._sync_transcript()

    # -- Step 2: identify load --------------------------------------------- #
    def _identify_load(self, text: str) -> str:
        numeric = self._settings.numeric_load_ids
        load_id = parsing.extract_load_id(text, numeric=numeric)
        if not load_id:
            if _CALLER_DONE_RE.match(text):
                # "No, that's okay" is an ANSWER, not a missing number. The
                # caller has declined to go on — usually right after being told
                # a load can't be sold — and re-asking for a number they just
                # declined to give is how a polite caller ends up hanging up on
                # us (observed live). Thank them and end it.
                self._note("Caller declined to continue at load lookup — "
                           "thanked them and closed the call.")
                self._finish(CallOutcome.NO_DEAL)
                return self._say(
                    "They're all set — nothing else they want looked up. Thank "
                    "them for calling and close the call warmly. One short "
                    "sentence, no questions.",
                    amounts=set(),
                )
            return self._say(
                "You didn't catch a load or reference number in what they just said. Ask "
                "for it — if they described a lane or a posting instead of a number, ask "
                "whether the posting shows a reference number. Do not invent a load, and "
                "do not read them the open list yet.",
                facts=("Example of the number format: "
                       + ("1303369." if numeric else "L1001.")),
                amounts=set(),
            )
        result = self._loads.lookup(load_id)

        # The agent NEVER reads out a list of other loads. A carrier ringing about
        # one specific posting is not shopping a list, and five load numbers spoken
        # down a phone line is the part that sounds like a machine — worse still
        # when the voice renders them "twenty five thirty five one thirty".
        #
        # So every unsellable branch says the truth about the load they asked for
        # and nothing more. `_unsellable` handles the follow-up and the cap.
        if result.out_of_scope:
            # Another office's freight is DECIDED, not misheard — no retry can
            # change whose desk it is, so unlike a plain miss this ends the call
            # on the first hit, with the same warm close a covered load gets.
            # Observed live: collapsed into "not on the board", it sent a caller
            # hunting through their posting for a number that could never work.
            self._note(
                f"Load {load_id} belongs to another office — outside this desk's "
                "scope. Thanked the caller and closed without offering alternatives."
            )
            self._finish(CallOutcome.NO_DEAL)
            return self._say(
                f"Load {load_id} is real but it's handled by a different Circle "
                f"desk, not this one, so you can't book it or transfer them to it. "
                f"Tell them that plainly, thank them for reaching out, and close "
                f"warmly. Do NOT offer another load, do NOT read out any other "
                f"load number, and do not ask what else they are looking for — "
                f"this call is finished. Two short sentences.",
                amounts=set(),
            )
        if not result.found:
            return self._unsellable(f"You have no load {load_id} on the board.")
        if not result.posted:
            return self._unsellable(
                f"Load {load_id} exists but isn't posted, so you can't book it — say "
                f"only that much.")
        if not result.available:
            # "Already covered" is a specific claim about the freight. It is only
            # made when the board actually says somebody has it — a load that is
            # merely not released yet gets the true, vaguer sentence instead. A
            # caller repeats what we tell them to the shipper.
            if result.load.status == LoadStatus.COVERED:
                # A covered load ENDS the call, and deliberately does not turn into
                # a pitch for something else. A carrier ringing about one specific
                # posting is not shopping a list, and reading five numbers at
                # somebody who wanted that lane is the part that sounds like a
                # machine. The desk's instruction is: tell them, thank them, done.
                #
                # `self.load` is set purely so the call record says which load this
                # was about. Nothing can be sold on it — the state is DONE and
                # `_load_revealed` stays False, so no load detail ever reaches the
                # composer.
                self.load = result.load
                self._note(
                    f"Load {load_id} is covered ({result.load.status.value}); told the "
                    "caller and closed the call without offering alternatives."
                )
                self._finish(CallOutcome.NO_DEAL)
                return self._say(
                    f"Load {load_id} is already covered — somebody else has taken it. "
                    f"Tell them that plainly, thank them for calling, and close warmly. "
                    f"Do NOT offer another load, do NOT read out any other load number, "
                    f"and do not ask what else they are looking for — this call is "
                    f"finished. Two short sentences.",
                    amounts=set(),
                )
            return self._unsellable(
                f"Load {load_id} is on your system but is not released for booking "
                f"right now, so you can't sell it. Say only that — that one isn't "
                f"available to book at the moment. Do NOT say it's covered, do NOT "
                f"guess why, and do NOT promise it will open up later.")

        self.load = result.load

        # Posted and open, but the board published no rate on it. There is no
        # honest number to open at, and an invented anchor is one the desk may be
        # held to — so a person picks this up. The carrier never hears that our
        # data is thin, only that someone else is taking it.
        if not result.load.is_quotable:
            self._note(
                f"Load {load_id} is posted and open but carries no usable Load Board "
                f"Rate (open_rate={result.load.open_rate}, "
                f"ceiling_rate={result.load.ceiling_rate}) — no anchor to quote, "
                "handed to a rep before any rate was discussed."
            )
            return self._transfer_and_say(reason="no_published_rate")

        # Valid, posted, open, quotable — but nothing about it comes out yet. Read
        # the number back first: the caller is holding a posting and needs to know
        # we're both looking at the same load before they hand over their MC.
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

    def _unsellable(self, what_to_say: str) -> str:
        """A load we can't sell them — told plainly, with no list of alternatives.

        The three branches that land here (never on the board, not posted, not
        released) are alike in one way that matters: NOBODY ELSE HAS THE FREIGHT.
        Nothing has been decided, so the caller is asked whether they have another
        number rather than being read our board. A covered load is different and
        ends the call outright — somebody does have that one.

        Capped, because removing the list removed the call's way forward. After
        `_MAX_LOAD_ASKS` numbers that don't work, the call is wrapped up warmly
        instead of looping. Three attempts, not one, because speech-to-text
        mishears digits regularly enough that the first miss is usually ours.
        """
        self._load_asks += 1
        if self._load_asks >= _MAX_LOAD_ASKS:
            self._note(
                f"{self._load_asks} load numbers in a row could not be sold — closed "
                "the call without offering alternatives."
            )
            self._finish(CallOutcome.NO_DEAL)
            return self._say(
                f"{what_to_say} That is the third number that hasn't worked, so wrap "
                f"the call up: say you can't get them on anything today, thank them "
                f"for calling, and close warmly. Do NOT offer or read out ANY other "
                f"load number, and do not ask for another number — this call is "
                f"finished. Two short sentences.",
                amounts=set(),
            )
        return self._say(
            f"{what_to_say} Then ask whether they have another number off the board. "
            f"Do NOT offer, suggest or read out ANY other load number — you are not "
            f"reading them a list, and you must not describe any other freight. One "
            f"short question.",
            amounts=set(),
        )

    # -- Hearing an MC/USDOT number the way a person does ------------------- #
    def _hear_identifier(self, text: str) -> str | None:
        """Resolve the caller's identifier from everything heard SO FAR.

        A rep doesn't need a clean six digits in one breath. They hold what they
        caught, add whatever comes next, and the moment it matches a carrier on
        their screen they're done. That's all this is: keep a running buffer, and
        let the carrier file pick between the possible readings of it — a caller
        who was cut off mid-number and one who started over produce different
        digit strings, and only one of them is going to be a real carrier.

        Returns the identifier to verify, or None if we still need more digits.
        """
        heard = parsing.heard_digits(text)

        # We narrowed to one carrier and asked "is this you?". Their answer is the
        # confirmation a rep actually works from — no point making someone recite
        # digits down a bad line when the name already settled it.
        if self._mc_narrowed is not None and not heard:
            if _AFFIRMS_RE.search(text.lower()):
                carrier = self._mc_narrowed
                number = re.sub(r"\D", "", carrier.mc_number or carrier.usdot_number)
                self._mc_digits = number
                return number
            if _DENIES_RE.search(text.lower()):
                # Wrong carrier, so the digits behind the guess were wrong too.
                # Keeping them would only narrow to the same wrong answer again.
                self._mc_digits = ""
                self._mc_narrowed = None
                return None

        readings = parsing.digit_readings(self._mc_digits, heard)

        # Whole number on file -> that's them, whichever reading produced it.
        for reading in readings:
            if len(reading) >= 4 and self._repo.get_carrier(reading) is not None:
                self._mc_digits = reading
                return reading

        # Nothing exact. Keep the longest reading that still narrows to a single
        # carrier, so the next digit or two finishes the job instead of starting
        # over. Failing that, keep the longest reading at all.
        best, matches = "", []
        for reading in readings:
            found = self._repo.carriers_matching_digits(reading)
            if len(found) == 1 and len(reading) > len(best):
                best, matches = reading, found
        self._mc_digits = best or (readings[0] if readings else self._mc_digits)
        self._mc_narrowed = matches[0] if matches else None
        return None

    def _chase_identifier(self, text: str) -> str:
        """Ask for what's still missing — never for the whole thing again."""
        self._mc_asks += 1
        held = self._mc_digits

        # Down to one carrier on a partial number: confirm by company name, which
        # is faster and far more reliable than collecting the last two digits.
        # Only worth doing once — if the name doesn't settle it, digits won't.
        if self._mc_narrowed is not None and self._mc_asks < _MAX_MC_ASKS:
            return self._say(
                "You've matched what you heard to one carrier on file. Read their company "
                "name back and ask if that's them — a name is easier to confirm over a bad "
                "line than digits are. Do NOT read the MC or USDOT digits back and do NOT "
                "ask them to repeat the number.",
                facts=f"Carrier you think this is: {self._mc_narrowed.legal_name}",
                amounts=set(),
            )

        if self._mc_asks >= _MAX_MC_ASKS:
            self._note(
                f"Could not capture an MC/USDOT number after {_MAX_MC_ASKS} attempts on "
                f"{self.load.load_id if self.load else 'no load'} — handed to a rep. "
                f"Digits held: {held or 'none'}. Last thing heard: \"{text.strip()}\"",
            )
            return self._transfer_and_say(reason="mc_not_captured")

        if held:
            # We have part of it. Read that part back and ask only for the rest —
            # this is the bit a caller actually finds normal, and it means their
            # first attempt wasn't wasted.
            spoken = formatting.spell_digits(held)
            return self._say(
                f"You caught part of their number and need the rest. Read back exactly "
                f"what you have — {spoken} — and ask what comes after it. Do NOT ask them "
                f"to start again, do NOT ask them to slow down or say it one digit at a "
                f"time, and do NOT say you didn't catch it: you caught most of it.",
                facts=f"Digits you already have: {spoken}",
                amounts=set(),
            )

        if self._mc_asks == 1:
            # Usually not a mishearing at all: they answered the load read-back
            # and haven't reached the number yet. Claiming to have missed
            # something they never said is what makes a caller lose confidence.
            return self._say(
                "There is no MC or USDOT number in what they just said — most likely they "
                "were answering your question about the load and haven't got to it yet. "
                "Just ask for their MC or USDOT number. Do NOT say you didn't catch it and "
                "do NOT ask them to repeat it: they haven't given it yet.",
                amounts=set(),
            )

        return self._say(
            "You got no digits out of that at all. Say the line is breaking up and ask for "
            "their MC or USDOT number once more. Ask normally — do NOT tell them to slow "
            "down or to say it one digit at a time; a rep just asks again.",
            amounts=set(),
        )

    # -- Step 3: verify carrier -------------------------------------------- #
    def _verify_carrier(self, text: str) -> str:
        number = self._hear_identifier(text)
        if number is None:
            return self._chase_identifier(text)
        # The load is passed in so the carrier is vetted FOR THIS LOAD, not just
        # in general: `self.load` is always set by now (the load number comes
        # before the MC), and without it a carrier can be cleared for freight they
        # hold no qualification to haul.
        result = self._verifier.verify(number, self.load)
        self.carrier = result.carrier
        who = result.carrier.legal_name if result.carrier else "not found"

        # A carrier who passed vetting but has never connected with us. The invite
        # goes to the address on their FILE, not to anything said on this call, and
        # it is sent before the handoff so a rep picks up a carrier who already has
        # the link in their inbox. Best-effort: a failed invite must not change
        # what the caller hears, and the note records which way it went.
        if result.invite_to_onboard and result.carrier is not None:
            invited = False
            if hasattr(self._repo, "invite_to_onboard"):
                invited = self._repo.invite_to_onboard(result.carrier)
            self._note(
                f"Carrier {who} passed vetting but is not connected with us "
                f"(Transport Pro said {result.carrier.raw_authority_status!r}). "
                + ("Highway connect invite sent to the address on their file."
                   if invited else
                   "Could NOT send the Highway invite — a rep has to do it.")
            )

        # DECLINE — the system of record gave us a definite answer and it wasn't
        # yes. Two different reasons, and the carrier is told a different thing
        # for each, but neither one hears which check it was.
        if result.action == VerificationAction.DECLINE:
            if result.reason == "authority_not_active":
                # The requirement is ACTIVE. Their record says INACTIVE or
                # SUSPENDED (or something we read as suspended, since an
                # unrecognised status fails closed), so there is no load
                # conversation and no rate conversation to have.
                self._note(
                    f"Carrier {who} (MC {result.carrier.mc_number or 'n/a'}, USDOT "
                    f"{result.carrier.usdot_number}) is in the system as "
                    f"{result.carrier.raw_authority_status!r} -> "
                    f"{result.carrier.authority_status.value}, not ACTIVE. Told they "
                    "do not meet the requirements. No load detail or rate discussed."
                )
                self._finish(CallOutcome.REJECTED)
                return self._say(
                    "Their company does not currently meet the requirements to work with "
                    "your brokerage, so you cannot go any further on this load with them — "
                    "no lane, no details, no rate. Tell them exactly that, in one plain "
                    "sentence: their company doesn't meet certain requirements to work with "
                    "you right now. Then close the call politely. Do NOT say which check "
                    "failed, do NOT mention authority, insurance, safety ratings or any "
                    "specific status, do not speculate about why, and do not suggest how "
                    "they might get around it or when to try again.",
                    amounts=set(),
                )
            # Vetting is fine; the desk simply isn't set up with this company.
            self._note(
                f"Carrier {who} (USDOT {result.carrier.usdot_number}) is not approved "
                "to work with Circle Logistics — declined."
            )
            self._finish(CallOutcome.REJECTED)
            return self._say(
                "Your brokerage isn't set up to work with their company, so you can't book "
                "this one with them. Tell them briefly and politely and close the call. Do "
                "NOT explain why, do not mention any list, and do not offer a workaround.",
                amounts=set(),
            )

        # HUMAN_REVIEW — we do NOT have a definite answer. Either the record
        # carries a status we couldn't read, or insurance is missing, or a soft
        # flag came up. None of these is something to accuse a carrier of, so a
        # person picks it up.
        if result.action == VerificationAction.HUMAN_REVIEW:
            self._note(
                f"Carrier {who} not cleared automatically: {result.reason}, flags "
                f"{list(result.risk_flags)} — routed to a rep, no rate discussed."
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

        self._note(
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
                "Give them the load, SHORT — the way a rep rattles it off, not the way a "
                "screen lists it. THREE SENTENCES AT MOST, and under about twelve seconds "
                "of speech.\n"
                + _PITCH_ESSENTIALS +
                "Do NOT read them the special requirements yet — that is the very next "
                "thing you will do. Finish by telling them there are a couple of specific "
                "requirements you need to run through. Say NOTHING about rate yet; that "
                "comes only once they have said they can meet those requirements.",
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
                "Give them the load and your number, SHORT — the way a rep rattles it "
                "off, not the way a screen lists it. THREE SENTENCES AT MOST, and under "
                "about twelve seconds of speech.\n"
                + _PITCH_ESSENTIALS +
                f"Then put YOUR number on it: you're asking ${opening} on this one. It is "
                f"YOUR asking rate, not theirs — \"I've got it at ${opening}\", never "
                f"\"you're at ${opening}\". Finish by asking if they want the load. They "
                "already know they're verified, so don't tell them again.",
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
        # A no is honoured whenever it comes, INCLUDING before the requirements
        # have been read. They have just heard the lane, the dates and the
        # equipment, so "we can't do that" is a decline of the load itself — making
        # them say it twice, once either side of a list they have already decided
        # against, is the opposite of listening.
        if _declines_requirements(text):
            self._note(
                f"Carrier {self.carrier.legal_name} can't meet the requirements on "
                f"{self.load.load_id} ({self.load.notes!r}) — not booked."
                + ("" if self._requirements_read
                   else " Declined before the requirements were read out."),
            )
            self._finish(CallOutcome.NO_DEAL)
            return self._say(
                "They've said they can't meet this load's requirements, so you can't book "
                "it with them. Say that without making them feel bad about it, leave the "
                "door open for the next one, and close the call. Two sentences, no rate.",
                amounts=set(),
            )

        # The pitch turn deliberately stops before the requirements, so the FIRST
        # time through here is where they are actually read. Splitting them off is
        # what keeps either turn short enough to follow: read together they ran to
        # 25 seconds of speech and hit the token limit mid-sentence.
        if not self._requirements_read:
            self._requirements_read = True
            return self._say(
                "Now cover this load's requirements from FACTS and ask outright whether "
                "they can do it. TWO SENTENCES, then the question.\n"
                "SAY every CONDITION THEY HAVE TO MEET — the trailer spec, tracking, "
                "check-in, paperwork, anything they must or must not do. Never drop or "
                "soften one of those: a driver who agrees without having heard "
                "'food grade' has agreed to something else. Compress them — run the "
                "trailer specs together in one breath ('clean dry food-grade van, swing "
                "doors, under ten years old') rather than giving each its own clause, "
                "and drop the reasons and the small print behind them: they need to "
                "know WHAT to meet, not why or how it gets checked.\n"
                "DO NOT read out the money terms — detention rates, layover pay, extra "
                "stop pay, TONU or fee disclaimers, reimbursement windows — and do not "
                "read out anything that is not theirs to agree to, like what the shipper "
                "might do or where else they might get sent. All of it is in FACTS and "
                "you answer it gladly IF ASKED, but reciting it makes this turn twice as "
                "long and none of it is something they need to agree to.\n"
                "Then stop and let them answer. No rate yet.",
                amounts=set(),
            )

        # Neither a yes nor a no. Historically this counted as agreement, which is
        # how "Hello." ended up on the record as a carrier confirming a food-grade
        # trailer. Consent has to be given, not merely not-withheld.
        if not _confirms_requirements(text):
            self._requirement_asks += 1
            if self._requirement_asks >= _MAX_REQUIREMENT_ASKS:
                self._note(
                    f"Carrier {self.carrier.legal_name} never confirmed the "
                    f"requirements on {self.load.load_id} after "
                    f"{self._requirement_asks} asks (last heard: {text.strip()!r}) — "
                    "handed to a rep rather than treated as agreement."
                )
                return self._transfer_and_say(reason="requirements_unconfirmed")
            return self._say(
                "They did not actually answer whether they can meet the requirements. "
                "Ask again, plainly and briefly — can they do it, yes or no. Do not "
                "re-read the whole list, do not assume they can, and say nothing about "
                "rate.",
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
            #
            # OUR OWN STANDING OFFER is speakable here, and has to be. "What's it
            # pay" is one of the commonest things a carrier says, and the rate is
            # deliberately absent from FACTS — the engine owns it, not the load —
            # so forbidding every figure left the model asked to answer a question
            # while banned from saying the only thing that answers it. It said the
            # number anyway, was rejected on all three attempts, and the call was
            # handed to a rep. Observed on a live call against load 2535130.
            #
            # ONLY `current_offer`, and never as `must_say`:
            #   * restating a number we already said is not a concession, and the
            #     engine's state is untouched by this branch — we have not moved.
            #   * any OTHER figure, theirs or invented, is still a breach. The
            #     ceiling and the agent's own authority live elsewhere on the
            #     engine and remain unspeakable.
            #   * not forced, so "how much does it weigh" gets an answer about
            #     weight rather than a rate nobody asked for.
            standing = int(self.neg.current_offer)
            return self._say(
                f"They didn't give you a rate. Deal with whatever they actually just "
                f"said first — if it's a question you can answer from FACTS, answer "
                f"it. If what they're asking is what the load pays, tell them your "
                f"number is ${standing} — the SAME number you already gave them, said "
                f"again. Do NOT move it, do not treat the question as a reason to go "
                f"higher, and name no other figure. If they asked about something "
                f"else entirely, answer that and leave the rate alone. Either way, "
                f"finish by asking what they need to get on this load.",
                amounts={standing},
            )

        self.state = CallState.NEGOTIATE
        return self._apply_negotiation(
            self.neg.evaluate(ask, carrier_final=_declares_final(text)), ask)

    def _apply_negotiation(self, result, ask: float) -> str:
        if result.decision == Decision.ACCEPT:
            return self._propose_booking(result.rate)

        if result.decision == Decision.REVIEW:
            self._note(
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
            "difference — you are not moving on this turn. Never reveal your max. "
            f"Do NOT ask them to 'do' or accept ${ask_i} — that is their own number, "
            "they already said it; you are asking them to come to yours."
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
                    f"You opened at ${opened} and have since come UP to ${rate}; they are "
                    f"still sitting on ${ask_i}. Point the imbalance out — you moved, they "
                    f"didn't — and ask them to come your way now. Not rude about it. You "
                    f"went UP from ${opened} to ${rate}: never describe your own move as "
                    f"coming down, and don't call it a long way. {no_new_number} You may "
                    f"also mention your opening ${opened}.",
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
                f"sell the load on this one non-price point: {lever}. Then ask for the "
                f"LOWEST number they can actually take — not whether they can do ${ask_i}, "
                f"which is the number they already gave you. Firm, friendly, no begging. "
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
            f"equipment — and tell them you'll send the booking confirmation over to sign, "
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
            self._note(
                f"Agreed ${int(self._agreed_rate)} on {self.load.load_id} but carrier "
                f"couldn't confirm the {self.load.pickup_date} pickup — needs a rep to rework.",
            )
            return self._transfer_and_say(reason="pickup_issue")

        # Pickup confirmed -> ask THEM where the booking link goes. We don't read
        # our file at them and call that a confirmation; whatever they say is
        # checked against the account afterwards, and has to match.
        self.state = CallState.CONFIRM_EMAIL
        return self._say(
            "They can cover the pickup. Ask which email address to send the booking "
            "confirmation to. YOU are sending it to THEM — never imply they send you "
            "anything. Do NOT invent, guess or suggest an address, and do not read one off "
            "their file: wait for them to say it. Don't ask for a driver name or a truck "
            "number. One short question.",
            amounts=set(),
        )

    def _confirm_email(self, text: str) -> str:
        """The carrier gives the address, and it has to already be on their account.

        This is the last gate before the words "you're booked", and it is a gate
        rather than a formality: the booking link goes to whatever address clears
        here, so an address that is NOT on the carrier's account in the system of
        record does not clear. That rules out the failure this exists to prevent —
        somebody who has learned an MC number talking us into sending the booking
        link to an address they control.

        So there are exactly three ways out of this state:

          * the address they said is on the account  -> booked, link goes there
          * they point at the account ("use the one  -> booked, link goes to the
            you've got") and we hold one               most recent one on file
          * anything else, after one more ask       -> NOT booked, a rep takes it

        The old behaviour — accept any address, append it to the file, confirm the
        booking anyway — is deliberately gone.
        """
        spoken = parsing.extract_email(text)
        # Read the file rather than trusting the copy taken at verification: on a
        # long call a rep may have added the address while we were talking.
        on_file = self._repo.carrier_emails(self.carrier.usdot_number)

        if spoken:
            if spoken.strip().lower() in on_file:
                return self._book_rate_con(
                    spoken.strip().lower(), "matched the carrier's account", text)
            # A real address, but not one we hold. Most often a mis-heard domain,
            # so it is worth one more ask before handing the call over.
            if self._email_asks < 1:
                self._email_asks += 1
                return self._say(
                    "That address is not the one on their account, so you cannot send the "
                    "booking to it. Say you have a different address on file for them and "
                    "ask them to confirm the one that's on the account — or to read theirs "
                    "back once more in case you misheard it. Do NOT read out any address "
                    "from their file, do NOT spell one for them, and do NOT accuse them of "
                    "anything: assume you misheard. One short question. Do not say they're "
                    "booked yet.",
                    facts=f"What you heard, which is NOT on their account: {spoken}",
                    amounts=set(),
                )
            return self._email_unverified(
                f"carrier gave {spoken!r}, which is not on their account", text)

        # "Just use the one you've got" — they're pointing at the account rather
        # than reciting it, which is the account address by definition.
        if on_file and _defers_to_file(text):
            return self._book_rate_con(
                on_file[-1], "carrier deferred to the address on their account", text)

        if self._email_asks < 1:
            self._email_asks += 1
            return self._say(
                "You didn't get a usable email address out of that. Say you didn't catch it "
                "and ask again for the best address for the booking confirmation. One short "
                "sentence. Do NOT invent, guess or suggest an address.",
                amounts=set(),
            )

        return self._email_unverified("no usable address given on the call", text)

    def _email_unverified(self, why: str, text: str) -> str:
        """Rate agreed, address not verified -> a person finishes it.

        Everything the desk needs is already recorded: the agreed rate is in the
        offer history and this note carries the reason. What the agent must not do
        is tell them they're booked, because nothing was booked.
        """
        self._note(
            f"NOT BOOKED on {self.load.load_id} at the agreed "
            f"${int(self._agreed_rate)}: {why}. Addresses on the account for "
            f"{self.carrier.legal_name}: "
            f"{', '.join(self._repo.carrier_emails(self.carrier.usdot_number)) or 'none'}. "
            f"Last thing heard: \"{text.strip()}\". Rate was agreed on the call — a rep "
            "can confirm the address and book it."
        )
        return self._transfer_and_say(reason="email_not_verified")

    def _book_rate_con(self, email: str, source: str, text: str) -> str:
        self._booking_email = email
        self._note(
            f"Booking {self.load.load_id} @ ${int(self._agreed_rate)} for "
            f"{self.carrier.legal_name} (MC {self.carrier.mc_number or 'n/a'}) — "
            f"booking link to {email} ({source}); raw: \"{text.strip()}\""
        )
        return self._finalize_booking()

    def _finalize_booking(self) -> str:
        """Put the booking in the system of record, THEN say it out loud.

        In that order, and only in that order. "You're booked" is the one sentence
        on this call that puts a truck on the road, and a carrier who hears it and
        then finds no load against their name shows up at a shipper for freight
        that isn't theirs. So if the write-back fails, nobody is told they're
        booked — the call goes to a rep with the rate and the address already in
        the note, which is a bad minute for us and a safe one for them.

        Two write paths, and which one is available decides what the carrier is
        told, because they are genuinely different promises:

          * a BOOKING LINK exists -> the load is not theirs until they open it and
            sign, so that is exactly what they hear. Not "you're booked".
          * no link endpoint      -> the rate is logged for a rep, and the older
            wording stands.

        When the link path is configured but fails, the call goes to a rep rather
        than falling back to `record_booking`: `POST /offer` may already have
        landed, and logging a second offer against the same load is how a lane ends
        up double-sold.
        """
        rate = int(self._agreed_rate)
        notes = (f"Booked by voice AI on call {self.call_id} at ${rate}. "
                 f"Truck empty: {self._empty_summary() or 'not captured'}")

        if self._can_issue_booking_link():
            return self._finalize_with_link(rate, notes)
        return self._finalize_with_logged_offer(rate, notes)

    def _can_issue_booking_link(self) -> bool:
        """True when this deployment can produce a link the carrier can act on.

        Both halves are needed: the offline SQLite repository has no
        `booking_link` at all, and the Transport Pro one has it but returns nothing
        useful without HappyRobot credentials.
        """
        return (hasattr(self._repo, "booking_link")
                and self._settings.uses_happyrobot)

    def _finalize_with_link(self, rate: int, notes: str) -> str:
        attempt = self._repo.booking_link(
            self.load,
            self.carrier,
            rate,
            email=self._booking_email,
            contact_name=self.carrier.legal_name,
            notes=notes,
        )
        if not attempt.link_issued:
            # Spell out whether the rate landed. A rep who assumes it didn't will
            # create a duplicate offer; one who assumes it did may never place it.
            self._note(
                f"NO booking link for {self.load.load_id} at ${rate} — nothing was "
                "said to the carrier about being booked. "
                + (f"The offer IS recorded (offer {attempt.offer_id}); a rep should "
                   "finish it from there rather than creating a second one."
                   if attempt.offer_recorded else
                   "Nothing was recorded against the load; a rep must place it.")
            )
            return self._transfer_and_say(reason="booking_link_failed")

        self._booking_link = attempt.url
        self._note(
            f"Booking link issued for {self.load.load_id} at ${rate} to "
            f"{self._booking_email} (offer {attempt.offer_id}). NOT yet signed — the "
            "load is not the carrier's until they complete it."
        )
        self._repo.book_load(self.load.load_id)
        self._finish(CallOutcome.BOOKED)
        return self._say(
            # Deliberately not "you're booked": they aren't, until they sign. The
            # urgency is real rather than a sales tactic — the load stays on the
            # board until the link is completed, so a slow carrier genuinely does
            # lose it to somebody else.
            f"They've agreed ${rate} and the booking link is on its way to "
            f"{self._booking_email}. Tell them it's going to that address — say the "
            f"address exactly as written, do not alter or improve it — and that they "
            f"need to open it and sign to lock the load in. Be clear the load is NOT "
            f"theirs until they finish it, and that if they leave it a while another "
            f"carrier can still take it, so do it now. Do NOT tell them they are "
            f"'booked' or 'confirmed' — they are not yet. Do not try to read out a "
            f"web address. Close warmly and briefly; they're a driver who wants to "
            f"get moving.",
            # The URL is deliberately absent from FACTS: a link cannot be spoken
            # down a phone, and anything in FACTS is something the model may say.
            facts=f"Booking link goes to: {self._booking_email}",
            amounts={rate},
            must_say=rate,
        )

    def _finalize_with_logged_offer(self, rate: int, notes: str) -> str:
        recorded = self._repo.record_booking(
            self.load,
            self.carrier,
            rate,
            email=self._booking_email,
            contact_name=self.carrier.legal_name,
            notes=notes,
        )
        if not recorded:
            self._note(
                f"Could NOT record the ${rate} booking for {self.load.load_id} in the "
                "system of record — nothing was said to the carrier about being booked. "
                "A rep must place this manually."
            )
            return self._transfer_and_say(reason="booking_write_failed")

        self._repo.book_load(self.load.load_id)
        self._finish(CallOutcome.BOOKED)
        return self._say(
            f"Confirm they're booked on this load at ${rate}. Tell them the booking "
            f"confirmation is going to {self._booking_email} — say that address exactly as "
            f"written, do not alter or improve it — and that they just need to open it and "
            f"sign to lock it in. Close warmly and briefly; they're a driver who wants to "
            f"get moving.",
            facts=f"Booking link goes to: {self._booking_email}",
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
        # Every branch below forbids claiming the carrier is verified. Observed on a
        # live call: handed over because the MC could not be captured at all, the
        # model opened with "Perfect, I've got you verified" — a flat untruth about
        # the one thing this call had failed to establish, and the sort a carrier
        # repeats to the rep who picks up.
        _NOT_VERIFIED = (
            " Do NOT say or imply they are verified, approved, cleared, set up or "
            "good to go — none of that has been established. Do not congratulate "
            "them and do not say 'perfect'."
        )
        if resolution.rep is None:
            return self._say(
                "No rep is free right now. Tell them you've logged a callback and someone "
                "will get straight back to them. Brief and apologetic without grovelling. "
                "Name NO dollar figure." + _NOT_VERIFIED,
                amounts=set(),
            )
        return self._say(
            f"Tell them you're putting them through to {resolution.rep.name}, who handles "
            f"this load, and to hold a moment. Name NO dollar figure." + _NOT_VERIFIED,
            facts=f"Rep taking the call: {resolution.rep.name}",
            amounts=set(),
        )

    # -- Above the agent's own authority -> hand it to a human -------------- #
    def _escalate(self, ask: float, result) -> str:
        """Their number is inside Max Buy but above what the agent spends on its
        own. A rep in this spot doesn't walk and doesn't cave — they go ask."""
        offers = ", ".join(f"${int(o)}" for o in self.neg.offers_made) or "n/a"
        self._note(
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
        self._note(
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

    def note_playback_cut(self, spoken_line: str) -> None:
        """The caller spoke over this line and its audio stopped mid-play.

        The transcript records what was COMPOSED; barge-in means the caller may
        have heard none of it. Observed live: a caller filling dead air with
        "hello?" cut off the very answer they were waiting for, and the record
        showed a line they never heard. The note keeps the audit honest without
        polluting the dialogue the composer reads.
        """
        self._repo.log_note(
            self.call_id,
            f'Caller spoke over this line — its audio was cut off mid-play, so '
            f'they may not have heard it: "{spoken_line}"')

    def abandon(self) -> None:
        """The line dropped before the call concluded — finalize the record.

        The transcript is only persisted at `end_call`, so a caller hanging up
        mid-call would otherwise leave an open row and LOSE everything said —
        and hangups are precisely the calls a desk wants to read back. Called by
        the telephony worker on disconnect (and safe to call twice: a call that
        already finished keeps its real outcome).
        """
        if self.state != CallState.DONE:
            self._finish(CallOutcome.ABANDONED)

    def summary(self) -> dict:
        return {
            "call_id": self.call_id,
            "outcome": self.outcome.value if self.outcome else None,
            "load_id": self.load.load_id if self.load else None,
            "carrier": self.carrier.legal_name if self.carrier else None,
            "turns": len(self.transcript),
            # A booked call where a link went out is NOT the same as one where the
            # rate was only logged: the first is waiting on the carrier's
            # signature, the second on a rep. `outcome` can't tell them apart, so
            # anything counting bookings should read this too.
            "booking_link_sent": self._booking_link is not None,
        }
