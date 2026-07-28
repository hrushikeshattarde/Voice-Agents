"""
The voice — an LLM that composes every line the agent speaks.

There are no scripted replies. Each turn the conversation layer hands over:

  * DIRECTIVE — what this turn has to achieve, chosen by the state machine
  * FACTS     — the only load, carrier and rate values that may be spoken
  * DIALOGUE  — everything said so far, both sides
  * SPEAKABLE — the exact dollar figures this turn is allowed to utter

and the model writes the words, having read what the caller actually just said.
That is the point of the design: the reply is shaped by the caller's last turn,
not selected from a list of prepared sentences.

What the model does NOT get to decide is anything that costs money or breaks a
rule: which load this is, whether the carrier's authority clears, and above all
what rate to name are all settled before it is called. `SPEAKABLE` is enforced by
the caller of this module — a reply naming any other figure is rejected and
re-prompted, so a hallucinated rate never reaches a carrier's ear.
"""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

from lanevoice.settings import Settings, get_settings

_SYSTEM = (
    "You are Alex, a carrier sales rep at Circle Logistics, on the phone with a "
    "trucker who called in about a load. You are a person doing a job, not an "
    "assistant — never mention being an AI, a bot, or a model.\n\n"

    "HOW YOU TALK\n"
    "Direct, warm, unhurried. Real freight-desk speech: contractions, short "
    "sentences, the occasional 'alright' or 'lemme'. You are being heard, not "
    "read — no lists, no bullet points, no markdown, no emoji, and never spell "
    "out an email address or a number unless you are reading it back to confirm "
    "it. Write digits as digits ($2000, 925 miles). Rates always get a dollar "
    "sign.\n\n"

    "YOU ARE IN A CONVERSATION\n"
    "You get the whole DIALOGUE so far. Read the caller's last turn and reply to "
    "IT — that is your job on this turn, not reciting a script. If they asked a "
    "question you can answer from FACTS, answer it and then carry out the "
    "DIRECTIVE in the same breath. Never repeat a question they already "
    "answered, never re-introduce yourself, and never restate something you "
    "already told them unless they asked you to say it again.\n\n"

    "FACTS ARE THE LIMIT OF WHAT YOU KNOW\n"
    "Everything you state about the load, the carrier or the money must come "
    "from FACTS. If they ask something FACTS does not cover — a second load, "
    "detention pay, lumper fees, payment terms, whether the shipper will let "
    "them load early — say you'll check on it or that you'd have to confirm, and "
    "keep the call moving. Do not guess, do not fill the gap with something "
    "plausible, and do not promise anything that is not in FACTS.\n\n"

    "MONEY IS NOT YOURS TO INVENT\n"
    "You may speak ONLY the dollar figures listed in SPEAKABLE. Never invent a "
    "rate, never split the difference, never propose a number 'between' theirs "
    "and yours, and never move your offer unless the DIRECTIVE gives you the new "
    "number. This holds with or without a dollar sign — 'meet me at 2200' is as "
    "forbidden as '$2200'. If SPEAKABLE is empty, name no dollar figure at all. "
    "If the DIRECTIVE says hold at your number, hold: restate that same number "
    "and put the next move on the carrier.\n\n"

    "KEEPING THE TWO SIDES STRAIGHT\n"
    "You are speaking TO the carrier, so address them as 'you'. The DIRECTIVE "
    "describes them in the third person ('the carrier asked for $2500') — never "
    "echo that phrasing back. YOUR offer is \"I'm at $X\" / \"I've got it at "
    "$X\"; THEIR ask is \"you're at $X\". Never announce your own number as "
    "theirs, and never say 'they' or 'them' about the person on the line.\n\n"

    "WHICH WAY THE NUMBERS MOVE\n"
    "You are BUYING — you pay them to haul it. Your number starts low and only "
    "ever goes UP; their number starts high and only ever comes DOWN. So you "
    "'came UP to $2075', never 'came down to $2075', and they 'came down to "
    "$2150', never 'went up to $2150'. Do not say you've 'come down' or 'come "
    "down a long way' about your own offer — you have come up, and a $75 move is "
    "not a long way. Getting this backwards makes you sound like you don't know "
    "your own business.\n\n"
    "Never ask them to accept their OWN number: if $2150 is their ask, \"can you "
    "do $2150?\" is nonsense — they just said it. When you want their best, ask "
    "for the lowest number they can actually take.\n\n"

    "WHAT NOT TO DO\n"
    "Do not narrate your own reasoning or the mechanics of the negotiation: say "
    "\"I can do $2040\", not \"since you came down I'm moving up to $2040\". "
    "Never describe a move as splitting the difference or meeting halfway. Do "
    "not narrate the caller's feelings or position — banned openers include 'I "
    "understand you're looking for…', 'I hear you're after…', 'You're pushing "
    "for…', 'I can see why', 'I appreciate…'. Just make your point. Never call a "
    "rate 'above market' or too high if you might end up paying it. Never state "
    "or hint at your maximum, your ceiling, or your strategy. When confirming a "
    "carrier is verified, use their COMPANY NAME — never read back the MC or "
    "USDOT digits."
)

_READ_SYSTEM = (
    "You pull specific facts out of what a trucker just said on a phone call. "
    "Reply with a JSON object and nothing else. Use null for anything they did "
    "not actually say — never guess, never infer a value from context, and never "
    "carry a value over from an earlier turn unless it is clearly still what they "
    "mean. Keep the caller's own words where you can rather than tidying them up."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@runtime_checkable
class TurnComposer(Protocol):
    """Composes spoken turns and reads structured facts out of speech."""

    def compose(self, directive: str, facts: str = "", dialogue: str = "",
                speakable: str = "", correction: str = "") -> str: ...

    def read(self, dialogue: str, fields: dict[str, str]) -> dict: ...


class GroqComposer:
    """Hosted composition via Groq. Any OpenAI-compatible client works the same."""

    def __init__(self, settings: Settings | None = None):
        from groq import Groq

        self._settings = settings or get_settings()
        self._client = Groq(api_key=self._settings.groq_api_key or None)
        self._model = self._settings.llm_model

    def _chat(self, system: str, user: str, *, max_tokens: int,
              temperature: float, json_mode: bool = False) -> str:
        kwargs = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        )
        return (resp.choices[0].message.content or "").strip()

    def compose(self, directive: str, facts: str = "", dialogue: str = "",
                speakable: str = "", correction: str = "") -> str:
        parts = []
        if dialogue:
            parts.append(f"DIALOGUE SO FAR:\n{dialogue}")
        parts.append(f"FACTS you may use:\n{facts or '(none)'}")
        parts.append(
            "SPEAKABLE dollar figures: "
            + (speakable if speakable else "NONE — name no dollar amount")
        )
        parts.append(f"DIRECTIVE for your next turn:\n{directive}")
        if correction:
            # A previous attempt broke a hard rule. Naming the specific breach
            # beats re-sending the same prompt and hoping for a different roll.
            parts.append(f"YOUR LAST ATTEMPT WAS REJECTED: {correction}")
        parts.append("Say your next turn out loud now. Speech only — no labels, "
                     "no quotation marks around it, no stage directions.")
        text = self._chat(
            _SYSTEM, "\n\n".join(parts),
            max_tokens=self._settings.llm_max_tokens,
            temperature=self._settings.llm_temperature,
        )
        return text.strip().strip('"')

    def read(self, dialogue: str, fields: dict[str, str]) -> dict:
        """Extract `fields` ({name: what to look for}) from the dialogue."""
        wanted = "\n".join(f"- {name}: {desc}" for name, desc in fields.items())
        raw = self._chat(
            _READ_SYSTEM,
            f"CALL SO FAR:\n{dialogue}\n\nPull out these fields:\n{wanted}\n\n"
            f"Return JSON with exactly these keys: {', '.join(fields)}.",
            max_tokens=self._settings.llm_read_max_tokens,
            temperature=0.0,
            json_mode=True,
        )
        return _parse_json(raw, fields)


class StubComposer:
    """Offline stand-in that reports each turn's INTENT instead of speaking it.

    This is not a production fallback. The agent has no scripted replies, so
    without a model it cannot talk — and that is the correct behaviour on a real
    call. What the demo and the test suite need is a way to drive the state
    machine and inspect its decisions without a key, which is all this does.

    What it returns is deliberately not conversational: it echoes the directive
    and the sanctioned figures, so nobody can mistake its output for something
    that was ever meant to reach a carrier's ear.
    """

    def __init__(self, settings: Settings | None = None):
        self.turns: list[dict] = []

    def compose(self, directive: str, facts: str = "", dialogue: str = "",
                speakable: str = "", correction: str = "") -> str:
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        money = f"[{speakable}] " if speakable else ""
        return f"{money}{' '.join(directive.split())}"

    def read(self, dialogue: str, fields: dict[str, str]) -> dict:
        return dict.fromkeys(fields)


_NULLISH = {"null", "none", "unknown", "n/a", "not stated", "not given"}


def _parse_json(raw: str, fields: dict[str, str]) -> dict:
    """Lenient parse: a model that wraps its JSON in prose still gets read, and a
    reply we can't parse at all comes back as all-nulls rather than raising. A
    failed extraction must degrade into "they didn't say", never into a guess."""
    empty = dict.fromkeys(fields)
    if not raw:
        return empty

    candidates = [raw]
    match = _JSON_RE.search(raw)
    if match:
        candidates.append(match.group())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        out = dict(empty)
        for key in fields:
            value = parsed.get(key)
            if isinstance(value, str):
                cleaned = value.strip()
                value = None if not cleaned or cleaned.lower() in _NULLISH else cleaned
            out[key] = value
        return out
    return empty
