"""OpenRouter hosted text-to-speech.

One POST per spoken turn to `/audio/speech`, which answers with raw audio bytes
rather than JSON. Two things about that endpoint shape this file:

* **The default response format is bare PCM** — no container, no header, so the
  sample rate is not in the payload. It arrives on the `Content-Type`
  (`audio/pcm;rate=24000;channels=1`) and that is what we read. Assuming a rate
  instead of parsing it is how a voice ends up chipmunked or slurred, and it
  would be inaudible in the transcript.

* **`voice` is provider-namespaced.** "alloy" is an OpenAI voice; Fish Audio has
  never heard of it. So `TTS_VOICE` is sent only when it is set, which lets a
  model with a default voice (Fish Audio has one) run with no voice configured
  at all rather than being handed a name it will reject.

The response is still decoded through soundfile when a container comes back
anyway — a model that ignores `response_format: pcm` and answers with WAV or MP3
must not have its header read as samples, which is the difference between a
loud burst of noise on a live call and an ordinary decode.
"""

from __future__ import annotations

import io
import re

import httpx
import numpy as np

from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

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

    Every TTS model tried here mangles '$1400' -> "$140" and reads 'L1002' oddly.
    We expand dollars to words and spell identifiers out digit-by-digit, separated
    by SPACES — hyphens are what made it read L1003 back as "L 1 0 0 0 0 3".
    Deliberately model-agnostic: these are failures of the general class, not of
    one vendor, so the rewrites stay in front of whichever voice is configured.
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


# `rate=` and `channels=` off an `audio/pcm;rate=24000;channels=1` Content-Type.
_CT_PARAM_RE = re.compile(r";\s*(rate|channels)\s*=\s*(\d+)", re.IGNORECASE)


def _pcm_params(content_type: str) -> tuple[int | None, int]:
    """`(sample_rate, channels)` as the response declared them.

    A missing rate comes back as None so the caller can fall back to the
    configured one and say so, rather than silently picking a number.
    """
    found = {k.lower(): int(v) for k, v in _CT_PARAM_RE.findall(content_type or "")}
    return found.get("rate"), max(1, found.get("channels", 1))


class OpenRouterTTS:
    """Interface: .synthesize(text) -> float32 mono np.ndarray, plus .sample_rate.

    The same two-member surface the LiveKit adapter in `telephony.worker` wraps,
    unchanged from the previous provider, so swapping vendors here touches nothing
    downstream.
    """

    def __init__(self, settings: Settings | None = None, *, transport: object = None):
        """`transport` is an httpx transport, injected by the tests so the real
        request shape and the PCM decode are exercised without a network."""
        self._settings = settings or get_settings()
        self._model = self._settings.tts_model
        self._voice = self._settings.tts_voice.strip()
        self._client = httpx.Client(
            base_url=self._settings.openrouter_base_url.rstrip("/"),
            timeout=httpx.Timeout(self._settings.tts_timeout),
            transport=transport,
            headers={
                "Authorization": f"Bearer {self._settings.openrouter_api_key}",
                "X-Title": "LaneVoice carrier sales agent",
            },
        )
        self.sample_rate = self._settings.tts_sample_rate
        # Warm up, and fail HERE rather than mid-call. A wrong model slug, a voice
        # this model doesn't have, or a key without audio access are all one 400
        # away, and finding out on the worker's first call means a carrier hears
        # silence. The worker's `prewarm` builds this before it takes any job, so
        # a broken voice config stops the process instead of a call.
        audio = self.synthesize("Ready.")
        if audio is None or len(audio) <= 1:
            raise RuntimeError(
                f"OpenRouter TTS ({self._model}) produced no audio. Check that "
                "TTS_MODEL is a text-to-speech model and that TTS_VOICE is one "
                "this model offers."
            )
        logger.info("OpenRouter TTS ready: %s / %s at %d Hz", self._model,
                    self._voice or "(model default voice)", self.sample_rate)

    def close(self) -> None:
        self._client.close()

    def synthesize(self, text: str) -> np.ndarray:
        body = {
            "model": self._model,
            # Say "$1400" as "fourteen hundred dollars", spell out load numbers.
            "input": speechify(text),
            # PCM skips a decode on both ends: no container to write, no MP3
            # frames to parse, and no dependence on the host's libsndfile being
            # new enough to read MP3 at all.
            "response_format": "pcm",
        }
        # Omitted entirely when unset — an empty `voice` is a 400, and Fish Audio
        # has a default voice of its own to fall back on.
        if self._voice:
            body["voice"] = self._voice

        response = self._client.post("/audio/speech", json=body)
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenRouter /audio/speech -> HTTP {response.status_code} for model "
                f"{self._model!r}"
                + (f", voice {self._voice!r}" if self._voice else " (no voice sent)")
                + f": {response.text[:300]}"
            )
        data = response.content
        if not data:
            return np.zeros(1, dtype=np.float32)

        content_type = response.headers.get("content-type", "")
        if "pcm" not in content_type.lower():
            # The model ignored `response_format` and sent a container. Reading a
            # WAV/MP3 header as samples is a burst of noise down the phone, so
            # decode it properly instead.
            return self._decode_container(data, content_type)
        return self._decode_pcm(data, content_type)

    # -- decoding ----------------------------------------------------------- #
    def _decode_pcm(self, data: bytes, content_type: str) -> np.ndarray:
        """Signed 16-bit little-endian PCM -> float32 mono in [-1, 1]."""
        rate, channels = _pcm_params(content_type)
        if rate is None:
            logger.warning(
                "OpenRouter returned PCM with no rate in its Content-Type (%r); "
                "falling back to TTS_SAMPLE_RATE=%d. If the voice sounds too fast "
                "or too slow, that is this.", content_type, self.sample_rate)
        else:
            self.sample_rate = rate

        # An odd byte count means a truncated final sample; dropping it is right,
        # while letting frombuffer raise would drop the whole turn.
        usable = len(data) - (len(data) % (2 * channels))
        if usable <= 0:
            return np.zeros(1, dtype=np.float32)
        audio = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio

    def _decode_container(self, data: bytes, content_type: str) -> np.ndarray:
        import soundfile as sf

        try:
            audio, rate = sf.read(io.BytesIO(data), dtype="float32")
        except Exception as exc:  # noqa: BLE001 - the reason matters more than the type
            raise RuntimeError(
                f"OpenRouter TTS returned {content_type!r}, which soundfile could "
                f"not decode ({exc}). Set TTS_MODEL to a model that honours "
                "response_format=pcm, or install a libsndfile that reads this "
                "format."
            ) from exc
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        self.sample_rate = rate
        return audio.astype(np.float32)
