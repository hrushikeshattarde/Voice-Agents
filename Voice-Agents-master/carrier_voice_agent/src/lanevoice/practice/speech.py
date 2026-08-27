"""
One-shot speech legs for voice practice — the rep talks, the customer talks back.

The phone worker streams (LiveKit, PCM blocks, barge-in). The browser needs none
of that: a practice turn is push-to-talk, so each leg is one plain HTTP exchange —
the recorded clip up to `/audio/transcriptions`, the persona's line down from
`/audio/speech`, handed back as a WAV an `<audio>` element plays natively. Both
legs run on the same OpenRouter key every deployment already has.

Reuse over duplication: synthesis goes through `OpenRouterTTS` (voice/tts.py),
which already owns the request shape, the PCM/container decode, and the
sample-rate-from-Content-Type handling; this module only adds the WAV container
the browser needs. Transcription has no one-shot client anywhere in the codebase
(the worker's STT lives inside the LiveKit plugin), so that call lives here —
same endpoint, same `STT_MODEL` and `STT_PROMPT` the phone line uses.
"""

from __future__ import annotations

import io
import wave

import httpx
import numpy as np

from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

# Whisper keys the decode on the filename extension, so the upload needs one
# that matches what the browser actually recorded.
_EXTENSIONS = {
    "audio/webm": "clip.webm",
    "audio/ogg": "clip.ogg",
    "audio/mp4": "clip.m4a",
    "audio/mpeg": "clip.mp3",
    "audio/wav": "clip.wav",
}

# One whole clip up, one transcript back — not a per-sentence turn like TTS, so
# this is deliberately roomier than TTS_TIMEOUT. A hung request here strands one
# practice turn in a browser, not a caller on a phone line.
_STT_TIMEOUT = 30.0


class PracticeSpeech:
    """Both speech legs behind two methods, so the session manager and the tests
    depend on `transcribe`/`synthesize` and never on a wire format."""

    def __init__(self, settings: Settings | None = None, *, transport: object = None):
        self._settings = settings or get_settings()
        if not self._settings.openrouter_api_key:
            raise RuntimeError(
                "Voice practice needs OpenRouter for speech-to-text and "
                "text-to-speech: set OPENROUTER_API_KEY.")
        self._transport = transport
        self._client = httpx.Client(
            base_url=self._settings.openrouter_base_url.rstrip("/"),
            timeout=httpx.Timeout(_STT_TIMEOUT),
            transport=transport,
            headers={
                "Authorization": f"Bearer {self._settings.openrouter_api_key}",
                "X-Title": "LaneVoice carrier sales agent",
            },
        )
        # Built on first use, not here: `OpenRouterTTS.__init__` warms up with a
        # real synthesis request (deliberately — see its docstring), and a
        # dashboard that only ever runs text sessions shouldn't pay for one.
        self._tts = None

    def close(self) -> None:
        self._client.close()
        if self._tts is not None:
            self._tts.close()

    def transcribe(self, audio: bytes, mime: str) -> str:
        """The rep's clip as text, via the same model the phone line hears with.

        Raises ValueError on a clip with no speech in it — the rep should hear
        "try again", not watch the customer answer a turn nobody took.
        """
        mime = (mime or "audio/webm").split(";")[0].strip().lower()
        data = {"model": self._settings.stt_model}
        if self._settings.stt_prompt:
            data["prompt"] = self._settings.stt_prompt
        response = self._client.post(
            "/audio/transcriptions",
            data=data,
            files={"file": (_EXTENSIONS.get(mime, "clip.webm"), audio, mime)},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter /audio/transcriptions -> HTTP {response.status_code} "
                f"for model {self._settings.stt_model!r}: {response.text[:300]}")
        text = str(response.json().get("text") or "").strip()
        if not text:
            raise ValueError(
                "Couldn't make out any speech in that clip — try again, a little "
                "closer to the mic.")
        return text

    def synthesize(self, text: str) -> tuple[bytes, str, float]:
        """`(wav_bytes, mime, seconds)` — the customer's line as playable audio.

        Seconds are exact (we made the audio), which is what the phase-2 talk-ratio
        metric wants on the customer side.
        """
        if self._tts is None:
            from lanevoice.voice.tts import OpenRouterTTS
            self._tts = OpenRouterTTS(self._settings, transport=self._transport)
        audio = self._tts.synthesize(text)     # float32 mono
        rate = self._tts.sample_rate
        return _to_wav(audio, rate), "audio/wav", round(len(audio) / rate, 2)


def _to_wav(audio: np.ndarray, rate: int) -> bytes:
    """Float32 mono -> a 16-bit WAV. The browser cannot be handed bare PCM the
    way the phone stack is: with no header there is no rate, and an <audio>
    element won't guess."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()
