"""Groq hosted text-to-speech."""

from __future__ import annotations

import io
import re

import numpy as np

from lanevoice.settings import Settings, get_settings

try:
    from num2words import num2words
except ImportError:  # pragma: no cover
    num2words = None


# Digits written out one at a time with hyphens — "L-1-0-0-3", "6-5-4-3-2-1".
# The voice treats the hyphens as part of the number and doubles the zeros, so
# "L-1-0-0-3" comes out of the speaker as "L 1 0 0 0 0 3" while the transcript
# looks perfectly correct. Spaces read cleanly. Each element has to be a LONE
# digit, which keeps phone numbers ("555-111-2222") and decimals ("2.50") out.
_SPELLED_RUN_RE = re.compile(r"\b([A-Za-z])?[-–—]?(\d(?:[-–—]\d){2,})\b")


def _respace_digits(match: re.Match) -> str:
    letter = match.group(1) or ""
    digits = " ".join(ch for ch in match.group(2) if ch.isdigit())
    return f"{letter} {digits}".strip()


def speechify(text: str) -> str:
    """Rewrite numbers/IDs so the TTS voice says them correctly.

    Orpheus (and most TTS) mangles '$1400' -> "$140" and reads 'L1002' oddly. We
    expand dollars to words and spell identifiers out digit-by-digit, separated by
    SPACES — hyphens are what made it read L1003 back as "L 1 0 0 0 0 3".
    """
    # Already spelled out, but with hyphens: L-1-0-0-3 -> "L 1 0 0 3"
    text = _SPELLED_RUN_RE.sub(_respace_digits, text)
    # Glued: L1002 -> "L 1 0 0 2"
    text = re.sub(r"\bL(\d{3,6})\b", lambda m: "L " + " ".join(m.group(1)), text)
    if num2words is not None:
        def _money(m):
            whole = int(m.group(1).replace(",", ""))
            cents = (m.group(2) or "").ljust(2, "0") if m.group(2) else ""
            if not cents or cents == "00":
                return f"{num2words(whole)} dollars"
            # Cents have to be caught here or the old pattern stopped at the
            # dollars and left the decimal stranded: "$2.50 a mile" reached the
            # voice as "two dollars.50 a mile".
            if whole < 100:      # a per-mile rate, said "two fifty a mile"
                return f"{num2words(whole)} {num2words(int(cents))}"
            return f"{num2words(whole)} dollars {num2words(int(cents))} cents"
        # $1,400 / $1400 / $2.50 -> spoken words
        text = re.sub(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?", _money, text)
    return text


class GroqTTS:
    """Interface: .synthesize(text) -> float32 mono np.ndarray, plus .sample_rate."""

    def __init__(self, settings: Settings | None = None):
        from groq import Groq

        self._settings = settings or get_settings()
        self._client = Groq(api_key=self._settings.groq_api_key or None)
        self._model = self._settings.tts_model
        self._voice = self._settings.tts_voice
        self.sample_rate = 24000
        wav = self.synthesize("Ready.")   # warm up + detect real sample rate
        if wav is None or len(wav) <= 1:
            raise RuntimeError("Groq TTS produced no audio")

    def synthesize(self, text: str) -> np.ndarray:
        import soundfile as sf

        resp = self._client.audio.speech.create(
            model=self._model,
            voice=self._voice,
            input=speechify(text),   # say "$1400" as "fourteen hundred dollars", etc.
            response_format="wav",
        )
        data = resp.read() if hasattr(resp, "read") else getattr(resp, "content", b"")
        if not data:
            return np.zeros(1, dtype=np.float32)
        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        self.sample_rate = sr
        return audio.astype(np.float32)
