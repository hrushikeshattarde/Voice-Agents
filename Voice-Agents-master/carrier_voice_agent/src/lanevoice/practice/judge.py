"""
The judge — an LLM grading the rep's call against a fixed rubric.

Two design rules keep the scores worth reading:

* **The rubric is fixed and shared.** Every session is scored on the same seven
  dimensions plus one persona-specific focus, so two reps (or one rep, two
  weeks apart) are compared on the same axes. A freeform "how did they do?"
  would drift too much to track progress against.

* **Evidence-bound.** Every dimension score must carry a verbatim quote from
  the transcript, and every improvement a concrete better line the rep could
  actually have said. Feedback that can't point at a moment is a horoscope.

The judge runs once, after the call, so latency is cheap — unlike the composer
it gets a long timeout and a big token budget. Its failure mode is graceful by
construction: an unparseable reply is retried once, then stored as a
`judge_error` on the report rather than crashing the end-of-session request.
The transcript is already on disk, so a failed judging can always be re-run.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from lanevoice.logging_config import get_logger
from lanevoice.practice.profiles import CustomerProfile
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

# The shared rubric. Keys are stable identifiers (they live in stored reports),
# so renaming one is a data migration, not an edit.
RUBRIC = {
    "opening": ("First impression: identified themselves, earned attention fast, "
                "sounded different from the tenth broker call of the day."),
    "discovery": ("Asked specific, open questions that surfaced the customer's real "
                  "situation, instead of pitching blind."),
    "listening": ("Each reply built on what the customer actually said — no re-asked "
                  "questions, no script rolling past an obvious signal."),
    "objection_handling": ("Met resistance with acknowledgment and substance, not "
                           "deflection, argument, or a canned rebuttal."),
    "value": ("Sold service and outcomes — coverage, recovery, reliability — rather "
              "than adjectives or price alone."),
    "composure": ("Stayed professional and unrattled under brush-offs, pushback or "
                  "hostility; no pleading, no bristling."),
    "closing": ("Drove toward a concrete next step with a date or a commitment on "
                "it; trial-closed instead of drifting."),
}
FOCUS_KEY = "focus"     # the persona-specific eighth dimension

_SYSTEM = (
    "You grade practice sales calls for a freight brokerage. A human sales rep "
    "cold-called a simulated customer; you score THE REP's performance only — the "
    "customer is a training device.\n\n"
    "You are a tough, fair sales coach. Specific beats kind: a 5 is honest work, "
    "an 8 is genuinely strong, a 10 is rare. Score only what the transcript "
    "shows — never what the rep probably meant. Every quote you cite must appear "
    "VERBATIM in the transcript. Every improvement must include a concrete line "
    "the rep could actually have said, in natural spoken English.\n\n"
    "Reply with a single JSON object and nothing else — no prose around it."
)

_RETRY_NOTE = (
    "\n\nYOUR LAST REPLY COULD NOT BE PARSED AS JSON. Reply again: one valid JSON "
    "object exactly matching the requested shape, and nothing else."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class JudgeChat(Protocol):
    """One provider call: the judge's JSON verdict for this prompt."""

    def __call__(self, system: str, user: str, *, max_tokens: int) -> str: ...


def build_judge_chat(settings: Settings | None = None) -> JudgeChat:
    """Provider-backed chat for judging — same provider stack as the persona,
    but with the judge's own timeout: it writes ~a page of JSON after the call
    is over, where the composer writes one sentence while someone waits."""
    settings = settings or get_settings()
    if not settings.use_llm:
        raise RuntimeError("Scoring needs a real model: set USE_LLM=true "
                           f"and {settings.llm_key_name}.")
    if not settings.llm_api_key:
        raise RuntimeError(f"Scoring needs a model key: set {settings.llm_key_name}.")

    from lanevoice.voice.composer import build_composer

    composer = build_composer(
        settings.model_copy(update={"llm_timeout": settings.practice_judge_timeout}))

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        text, truncated = composer._chat(
            system, user, max_tokens=max_tokens,
            temperature=composer._temperature, json_mode=True)
        if truncated:
            # Truncated JSON never parses; one retry asking for brevity, exactly
            # the composer's own recovery.
            logger.warning("Judge verdict hit the %d-token limit; retrying.", max_tokens)
            text, _ = composer._chat(
                system, user + "\n\nBe MUCH more concise this time — shorter "
                "quotes, shorter comments, same JSON shape.",
                max_tokens=max_tokens,
                temperature=composer._temperature, json_mode=True)
        return text

    return chat


def score_session(profile: CustomerProfile, transcript: list[list[str]],
                  chat: JudgeChat, settings: Settings | None = None) -> dict:
    """The report body for one finished session: normalized scores, strengths,
    improvements, win-condition verdict. On an unparseable judge, a dict with
    `judge_error` set — never an exception for bad model output. (Provider
    errors DO raise; the caller decides what a dead judge means.)"""
    settings = settings or get_settings()
    user = _prompt(profile, transcript)
    raw = chat(_SYSTEM, user, max_tokens=settings.practice_judge_max_tokens)
    parsed = _parse(raw)
    if parsed is None:
        raw = chat(_SYSTEM, user + _RETRY_NOTE,
                   max_tokens=settings.practice_judge_max_tokens)
        parsed = _parse(raw)
    if parsed is None:
        logger.error("Judge reply unparseable twice; storing the failure.")
        return {"judge_error": "the judge's reply could not be parsed as JSON",
                "raw": (raw or "")[:2000]}
    return _normalise(parsed)


# ------------------------------------------------------------------ internals #
def _prompt(p: CustomerProfile, transcript: list[list[str]]) -> str:
    facts = "\n".join(f"- {f}" for f in p.hidden_facts)
    rubric = "\n".join(f"- {key}: {desc}" for key, desc in RUBRIC.items())
    convo = "\n".join(
        f"{'REP' if who == 'rep' else 'CUSTOMER'}: {line}" for who, line in transcript)
    keys = [*RUBRIC, FOCUS_KEY]
    return (
        "THE CUSTOMER THE REP WAS PITCHING\n"
        f"{p.persona_name}, {p.title} at {p.company} ({p.vertical}).\n"
        f"Mood on this call: {p.disposition}\n\n"
        "What was privately true at this company — the rep could only learn these "
        f"by asking good questions:\n{facts}\n\n"
        f"THE REP'S GOAL ON THIS CALL\n{p.win_condition}\n"
        "Judge win_condition_met strictly: a vague 'sure, sometime' is NOT a win.\n\n"
        "RUBRIC — score each dimension 0-10 (0 absent or harmful, 5 adequate, "
        f"8 strong, 10 exceptional):\n{rubric}\n"
        f"- {FOCUS_KEY}: For THIS customer specifically — {p.rubric_focus}\n\n"
        f"TRANSCRIPT\n{convo}\n\n"
        "Return JSON with exactly this shape:\n"
        "{\"scores\": {" + ", ".join(f'"{k}": {{"score": 0, "quote": "verbatim '
        'moment", "comment": "one sentence"}}' for k in keys[:1]) +
        ", ... one entry for EVERY key: " + ", ".join(keys) + "},\n"
        " \"win_condition_met\": true|false,\n"
        " \"win_evidence\": \"one sentence pointing at the deciding moment\",\n"
        " \"strengths\": [\"2-3 things worth keeping, each tied to a moment\"],\n"
        " \"improvements\": [{\"what\": \"the habit to change\", \"why\": \"the cost "
        "it had on THIS call\", \"quote\": \"the verbatim moment\", \"better_line\": "
        "\"what to say instead, in spoken English\"} — 2 to 4 of these],\n"
        " \"summary\": \"2-3 sentences straight to the rep, second person\"}"
    )


def _parse(raw: str) -> dict | None:
    """Lenient, like the composer's `_parse_json`: a verdict wrapped in prose
    still counts; anything else is None, never an exception."""
    if not raw:
        return None
    candidates = [raw]
    match = _JSON_RE.search(raw)
    if match:
        candidates.append(match.group())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("scores"), dict):
            return parsed
    return None


def _normalise(parsed: dict) -> dict:
    """Clamp and shape whatever the model sent into the stored report contract.
    The overall is computed HERE, not asked of the model — a mean the code takes
    can't quietly disagree with the per-dimension scores it came from."""
    scores: dict[str, dict] = {}
    for key in [*RUBRIC, FOCUS_KEY]:
        entry = parsed["scores"].get(key)
        entry = entry if isinstance(entry, dict) else {}
        value = entry.get("score")
        scores[key] = {
            "score": max(0, min(10, round(value))) if isinstance(value, int | float) else None,
            "quote": str(entry.get("quote") or "")[:300],
            "comment": str(entry.get("comment") or "")[:400],
        }
    valid = [s["score"] for s in scores.values() if s["score"] is not None]
    improvements = []
    for item in parsed.get("improvements") or []:
        if isinstance(item, dict):
            improvements.append({
                "what": str(item.get("what") or "")[:300],
                "why": str(item.get("why") or "")[:400],
                "quote": str(item.get("quote") or "")[:300],
                "better_line": str(item.get("better_line") or "")[:400],
            })
    return {
        "overall": round(sum(valid) / len(valid), 1) if valid else None,
        "scores": scores,
        "win_condition_met": bool(parsed.get("win_condition_met")),
        "win_evidence": str(parsed.get("win_evidence") or "")[:400],
        "strengths": [str(s)[:300] for s in (parsed.get("strengths") or [])
                      if isinstance(s, str)][:4],
        "improvements": improvements[:4],
        "summary": str(parsed.get("summary") or "")[:1000],
    }
