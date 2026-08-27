"""
The vocal-delivery judge — a model that HEARS the rep, not one that reads them.

The conversational judge (`judge.py`) reads the transcript; tone, hesitation
and monotone don't survive transcription. So voice sessions get a second
verdict from a model that accepts audio input. No Claude model does, which
makes this the one place practice reaches past the composer's provider —
chosen by probe (2026-08-18): gemini-3.7-flash heard a real clip, transcribed
it, and commented on pitch and hesitation, at ~$0.002 a session.

Same discipline as the text judge: fixed dimensions so reps are comparable,
scores clamped in code, an overall the model never gets to declare, and a
failure that lands as `delivery_error` on the report rather than anywhere near
a 500. And the same honesty about drift: perceived tone wobbles run to run,
which is why `acoustics.py` sits alongside with numbers that don't.
"""

from __future__ import annotations

import base64
import json
import re
import wave
from pathlib import Path

import httpx

from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

DELIVERY_RUBRIC = {
    "confidence": "Assured and settled, or hesitant — nervous pitch, trailing ends, "
                  "apologetic tone.",
    "clarity": "Enunciation and intelligibility: could a distracted customer on a "
               "cheap speakerphone follow every word?",
    "energy": "Alive and engaged, or flat — the voice a customer wants to keep "
              "listening to versus the one they wait out.",
    "pace": "Controlled and conversational, or rushed/dragging; room for the "
            "customer to get in.",
    "warmth": "Personable and human, or scripted and robotic.",
}

# The audio the judge hears is capped: quality over quantity, and audio tokens
# are the whole bill. 90 seconds of a rep is plenty to hear how they sound.
_MAX_JUDGED_SECS = 90.0
# 2000, measured: a real verdict ran past an 800 budget and was cut off
# mid-coaching — same lesson PRACTICE_JUDGE_MAX_TOKENS already learned. A
# truncated verdict is retried once with a brevity instruction.
_MAX_TOKENS = 2000
_TIMEOUT = 90.0

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_PROMPT = (
    "You are judging the VOCAL DELIVERY of a freight-brokerage sales rep on a "
    "practice cold call. The attached clips are the REP's side only, in call "
    "order. Judge the SOUND, not the sales strategy — a different judge owns "
    "the words.\n\n"
    "Score each dimension 0-10 (0 absent or harmful, 5 adequate, 8 strong, 10 "
    "exceptional), with a comment about what you HEAR — pitch, pace, hesitation, "
    "articulation, energy:\n"
    + "\n".join(f"- {key}: {desc}" for key, desc in DELIVERY_RUBRIC.items()) +
    "\n\nReturn ONLY a JSON object:\n"
    '{"scores": {"confidence": {"score": 0, "comment": ""}, "clarity": {...}, '
    '"energy": {...}, "pace": {...}, "warmth": {...}},\n'
    ' "coaching": ["1-3 specific vocal habits to work on, each tied to what you '
    'heard"]}'
)


class DeliveryJudge:
    """One audio-model call per scored voice session: clips in, verdict out."""

    def __init__(self, settings: Settings | None = None, *, transport: object = None):
        self._settings = settings or get_settings()
        if not self._settings.practice_delivery_model:
            raise RuntimeError("Vocal-delivery judging is off: PRACTICE_DELIVERY_MODEL "
                               "is empty.")
        if not self._settings.openrouter_api_key:
            raise RuntimeError("Vocal-delivery judging needs OPENROUTER_API_KEY.")
        self._model = self._settings.practice_delivery_model
        self._client = httpx.Client(
            base_url=self._settings.openrouter_base_url.rstrip("/"),
            timeout=httpx.Timeout(_TIMEOUT),
            transport=transport,
            headers={
                "Authorization": f"Bearer {self._settings.openrouter_api_key}",
                "X-Title": "LaneVoice carrier sales agent",
            },
        )

    def close(self) -> None:
        self._client.close()

    def score(self, clip_paths: list[Path]) -> dict:
        """The delivery verdict for a session's rep clips. `delivery_error` on a
        model that can't be parsed or clips that can't be heard — never raises
        for bad output. (Transport/HTTP failures DO raise; the session manager
        wraps them the same way it wraps the text judge.)"""
        parts = [{"type": "text", "text": _PROMPT}]
        judged = 0.0
        for path in _select_clips(clip_paths):
            judged += _wav_secs(path)
            parts.append({"type": "input_audio", "input_audio": {
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "format": "wav"}})
        if len(parts) == 1:
            return {"delivery_error": "no judgeable audio clips for this session"}

        raw, truncated = self._chat(parts)
        if truncated:
            # Truncated JSON never parses; one retry asking for brevity — the
            # same recovery every other model call in this package uses.
            logger.warning("Delivery verdict hit the %d-token limit; retrying.",
                           _MAX_TOKENS)
            brief = [*parts]
            brief[0] = {"type": "text", "text": parts[0]["text"] +
                        "\n\nBe MUCH more concise — shorter comments, same JSON shape."}
            raw, _ = self._chat(brief)
        parsed = _parse(raw)
        if parsed is None:
            logger.error("Delivery verdict unparseable; storing the failure.")
            return {"delivery_error": "the delivery judge's reply could not be "
                                      "parsed as JSON", "raw": raw[:1000]}
        verdict = _normalise(parsed)
        verdict["judged_secs"] = round(judged, 1)
        verdict["model"] = self._model
        return verdict

    def _chat(self, parts: list[dict]) -> tuple[str, bool]:
        """`(text, truncated)` for one verdict request."""
        response = self._client.post("/chat/completions", json={
            "model": self._model,
            "messages": [{"role": "user", "content": parts}],
            "max_tokens": _MAX_TOKENS,
        })
        body = response.json()
        if response.status_code >= 400 or "error" in body:
            raise RuntimeError(
                f"OpenRouter /chat/completions -> HTTP {response.status_code} for "
                f"delivery model {self._model!r}: {str(body.get('error') or body)[:300]}")
        choice = body["choices"][0]
        return ((choice["message"].get("content") or "").strip(),
                choice.get("finish_reason") == "length")


def _select_clips(paths: list[Path]) -> list[Path]:
    """WAV clips in call order until the budget is spent. The OPENING turns
    matter most for delivery coaching (that's where nerves live), so selection
    is front-loaded by construction."""
    chosen, total = [], 0.0
    for path in paths:
        if path.suffix.lower() != ".wav" or not path.exists():
            continue
        secs = _wav_secs(path)
        if not secs:
            continue
        if chosen and total + secs > _MAX_JUDGED_SECS:
            break
        chosen.append(path)
        total += secs
    return chosen


def _wav_secs(path: Path) -> float:
    try:
        with wave.open(str(path)) as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else 0.0
    except Exception:  # noqa: BLE001 - an unreadable clip is a skip, not a crash
        return 0.0


def _parse(raw: str) -> dict | None:
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
    scores: dict[str, dict] = {}
    for key in DELIVERY_RUBRIC:
        entry = parsed["scores"].get(key)
        entry = entry if isinstance(entry, dict) else {}
        value = entry.get("score")
        scores[key] = {
            "score": max(0, min(10, round(value))) if isinstance(value, int | float) else None,
            "comment": str(entry.get("comment") or "")[:400],
        }
    valid = [s["score"] for s in scores.values() if s["score"] is not None]
    return {
        "overall": round(sum(valid) / len(valid), 1) if valid else None,
        "scores": scores,
        "coaching": [str(c)[:300] for c in (parsed.get("coaching") or [])
                     if isinstance(c, str)][:3],
    }
