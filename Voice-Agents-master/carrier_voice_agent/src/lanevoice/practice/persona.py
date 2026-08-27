"""
The customer's voice — an LLM playing one profile, in character, turn by turn.

This is the mirror image of `voice/composer.py`: there the model plays OUR rep
under hard guardrails about money; here it plays THEIR customer, and the only
hard rule is staying in character. Everything that makes the customer worth
practicing against — the mood, the buried pain, what earns warmth and what
earns the click — comes from the profile TOML, not from code.

The provider plumbing is not duplicated. The composer classes already solve
provider selection, the temperature allowlist, truncation detection and
OpenRouter's 200-with-error body shape behind one method, `_chat` — so
`build_persona_chat` reaches for that method rather than growing a second
client that would drift from the first.
"""

from __future__ import annotations

from typing import Protocol

from lanevoice.logging_config import get_logger
from lanevoice.practice.profiles import CustomerProfile
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

# The model appends this to its final line when the customer ends the call.
# Stripped before anything reaches the rep's screen.
HANGUP_TOKEN = "[HANGUP]"

# Appended when a reply was cut off by the token limit — same recovery the
# composer uses: name the problem, ask for fewer words, one retry only.
_TOO_LONG = (
    "\n\nYOUR LAST ATTEMPT RAN PAST THE LENGTH LIMIT and was cut off mid-sentence. "
    "Say it again in far fewer words — you are a person on a phone, not a narrator."
)


class ChatFn(Protocol):
    """One provider call: the persona's next line for this prompt."""

    def __call__(self, system: str, user: str, *, max_tokens: int) -> str: ...


def build_persona_chat(settings: Settings | None = None) -> ChatFn:
    """Provider-backed chat for practice, or a refusal that names the fix.

    Practice has no offline mode: the sales agent's stub can echo directives
    because tests assert on the state machine, but a customer with no model
    cannot hold a conversation worth practicing against. So instead of quietly
    degrading, this raises — and the dashboard turns the message into a 400
    that tells the operator which setting to fill.
    """
    settings = settings or get_settings()
    if not settings.use_llm:
        raise RuntimeError(
            "Practice mode needs a real model: set USE_LLM=true "
            f"and {settings.llm_key_name}.")
    if not settings.llm_api_key:
        raise RuntimeError(f"Practice mode needs a model key: set {settings.llm_key_name}.")

    from lanevoice.voice.composer import build_composer

    composer = build_composer(settings)

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        text, truncated = composer._chat(
            system, user, max_tokens=max_tokens, temperature=composer._temperature)
        if truncated:
            logger.warning("Persona line hit the %d-token limit; retrying shorter.",
                           max_tokens)
            text, truncated = composer._chat(
                system, user + _TOO_LONG,
                max_tokens=max_tokens, temperature=composer._temperature)
            if truncated:
                # Half a sentence on screen reads as a broken product, and the
                # customer having to go is perfectly in character for every
                # profile shipped.
                logger.error("Persona line cut off twice at %d tokens; "
                             "hanging up instead of showing half a sentence.", max_tokens)
                return f"Sorry — I've got to run. {HANGUP_TOKEN}"
        return text

    return chat


class CustomerPersona:
    """Plays one profile for one session: profile in, next line out."""

    def __init__(self, profile: CustomerProfile, chat: ChatFn,
                 settings: Settings | None = None):
        self._profile = profile
        self._chat = chat
        self._settings = settings or get_settings()
        self._system = _system_prompt(profile)

    def opening_line(self) -> str:
        """How the customer answers the phone — verbatim from the profile, so
        every rep practicing a given mood starts from the identical first beat."""
        return self._profile.opening_line

    def reply(self, transcript: list[list[str]], rep_text: str) -> tuple[str, bool]:
        """`(line, hung_up)` — the customer's next line, given the call so far.

        `transcript` is the persisted [["rep"|"customer", line], ...] shape and
        does NOT yet include `rep_text`, which is the turn being answered.
        """
        convo = "\n".join(
            f"{'REP' if who == 'rep' else 'YOU'}: {line}" for who, line in transcript)
        user = (
            f"THE CALL SO FAR:\n{convo}\n\n"
            f"REP: {rep_text}\n\n"
            "Say your next line out loud now, as the customer. Speech only — no "
            "labels, no quotation marks around it, no stage directions."
        )
        raw = self._chat(self._system, user,
                         max_tokens=self._settings.practice_reply_max_tokens)
        if not raw.strip():
            # An empty line has no in-character recovery; make the rep's retry
            # explicit rather than showing a customer who silently said nothing.
            raise RuntimeError("The persona model returned nothing — send the turn again.")
        hung_up = HANGUP_TOKEN in raw
        line = raw.replace(HANGUP_TOKEN, "").strip().strip('"')
        return line, hung_up


def _system_prompt(p: CustomerProfile) -> str:
    """The whole character, assembled from the profile.

    Ordering mirrors the composer's prompt discipline: identity first, then the
    mood, then the private material with explicit rules about when it may
    surface. The hidden facts are the practice content — leaking them
    unprompted would hand the rep the discovery they were supposed to earn.
    """
    facts = "\n".join(f"- {f}" for f in p.hidden_facts)
    objections = "\n".join(f"- {o}" for o in p.objections)
    warms = "\n".join(f"- {w}" for w in p.warms_to)
    triggers = "\n".join(f"- {t}" for t in p.hangup_triggers)
    return (
        f"You are {p.persona_name}, {p.title} at {p.company} ({p.vertical}). A "
        "freight brokerage sales rep has cold-called you at work. You are a real "
        "person doing a real job — never an assistant, never an AI, never a model. "
        "You never break character; if asked whether you're a bot, react the way a "
        "busy customer actually would.\n\n"

        f"YOUR MOOD ON THIS CALL\n{p.disposition}\n\n"

        f"HOW YOU TALK\n{p.speech_style}\n"
        "This is a phone call: one conversational turn at a time, usually one or "
        "two sentences. No lists, no markdown, no stage directions, no narrating "
        "your feelings — just what you'd say into the phone.\n\n"

        f"WHAT'S ACTUALLY GOING ON AT {p.company.upper()} (PRIVATE)\n{facts}\n"
        "These are private. Never volunteer them. Reveal one — one at a time, "
        "reluctantly — only when the rep asks a good, specific question that would "
        "naturally surface it. A lazy question ('tell me about your shipping') "
        "earns a vague answer, not your problems.\n\n"

        f"YOUR OBJECTIONS\nRaise these naturally, when the conversation gives cause:\n"
        f"{objections}\n\n"

        f"WHAT WINS YOU OVER\nYou warm up — a little at a time, never all at once — "
        f"when the rep does these:\n{warms}\n"
        "Warming up means giving them more room, not buying. You concede your goal — "
        f"{p.win_condition} — ONLY if the rep has genuinely earned it over the call. "
        "If they never earn it, they never get it.\n\n"

        f"WHAT ENDS THE CALL\n{triggers}\n"
        "When you decide the call is over — a trigger fired, you gave the rep what "
        "they earned, or it has simply run its course — say your final line and "
        f"append the exact token {HANGUP_TOKEN} at the very end."
    )
