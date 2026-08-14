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
from collections.abc import Callable, Iterator

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


# How much audio to hand downstream at a time. Raw PCM has no framing so the size
# is ours to pick: small enough that the first block leaves as soon as it exists,
# large enough that a 22-second utterance is ~30 pushes rather than the ~190
# network chunks it arrives in. 80ms is inaudible as granularity.
_BLOCK_SECONDS = 0.08


def _downmix(pcm: bytes, channels: int) -> bytes:
    """Frame-aligned 16-bit PCM -> mono. A no-op on the mono path, which is every
    model configured here; present because a stereo stream read as mono is not a
    subtle bug, it is the voice at half speed."""
    if channels <= 1:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)
    return np.clip(samples.mean(axis=1), -32768, 32767).astype("<i2").tobytes()


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

    def _request_body(self, text: str) -> dict:
        body = {
            "model": self._model,
            # Say "$1400" as "fourteen hundred dollars", spell out load numbers.
            "input": speechify(text),
            # PCM skips a decode on both ends: no container to write, no MP3
            # frames to parse, and no dependence on the host's libsndfile being
            # new enough to read MP3 at all. It is ALSO what makes streaming
            # possible — see `stream_pcm`.
            "response_format": "pcm",
        }
        # Omitted entirely when unset — an empty `voice` is a 400, and Fish Audio
        # has a default voice of its own to fall back on.
        if self._voice:
            body["voice"] = self._voice
        return body

    def _http_error(self, status: int, detail: str) -> RuntimeError:
        return RuntimeError(
            f"OpenRouter /audio/speech -> HTTP {status} for model {self._model!r}"
            + (f", voice {self._voice!r}" if self._voice else " (no voice sent)")
            + f": {detail[:300]}"
        )

    def stream_pcm(self, text: str,
                   stop: Callable[[], bool] | None = None) -> Iterator[bytes]:
        """Yield 16-bit mono PCM as it arrives, instead of after it all has.

        WHY THIS IS POSSIBLE AT ALL: raw PCM has no header and no framing, so any
        prefix of the byte stream is already playable audio. A container could not
        be streamed this way — its header has to be read before a single sample
        means anything — which is why the non-PCM branch below still buffers.

        WHAT IT ACTUALLY BUYS. A request splits into the provider GENERATING (no
        bytes yet) and then the body TRANSFERRING. Streaming removes the second
        part from the caller's wait — playback starts on the first block instead of
        the last. Two runs, hours apart, on the same gateway:

            utterance     audio   generation   transfer      utterance    audio  gen   xfer
            long pitch    22.2s        2.14s      0.64s      pitch        17.9s  1.09s  0.33s
            short turn     5.1s        2.03s      0.11s      short turn    6.4s  0.97s  0.20s

        The absolute numbers move about twofold with gateway load, so re-measure
        (`tools/measure_latency.py --tts`) rather than trusting either column. What
        held across both runs is the SHAPE, and it is what justifies this design:

          * generation barely depends on utterance length (2.14 vs 2.03; 1.09 vs
            0.97) — it is close to a fixed floor per REQUEST;
          * transfer scales with length, and is the streamable part.

        So the honest saving here is 0.1-0.65s, biggest on the long load pitch.
        It also means SPLITTING THE TEXT INTO SENTENCES WOULD BE A MISTAKE: each
        chunk would pay the generation floor again, so three chunks would pay it
        three times to save one transfer. The win is streaming the body, not
        cutting up the input. (It also means the 0.12x-of-real-time figure in
        `settings.py` only holds for long utterances — a short turn pays the same
        floor as a long one.)

        `stop` is polled between blocks so an interrupted turn closes the
        connection instead of streaming into a queue nobody is reading.
        """
        with self._client.stream(
                "POST", "/audio/speech", json=self._request_body(text)) as response:
            if response.status_code >= 400:
                response.read()
                raise self._http_error(response.status_code, response.text)

            content_type = response.headers.get("content-type", "")
            if "pcm" not in content_type.lower():
                # The model ignored `response_format` and sent a container.
                # Reading a WAV/MP3 header as samples is a burst of noise down the
                # phone, so decode it properly — buffered, necessarily.
                audio = self._decode_container(response.read(), content_type)
                if len(audio):
                    yield (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                return

            rate, channels = _pcm_params(content_type)
            if rate is None:
                logger.warning(
                    "OpenRouter returned PCM with no rate in its Content-Type (%r); "
                    "falling back to TTS_SAMPLE_RATE=%d. If the voice sounds too "
                    "fast or too slow, that is this.", content_type, self.sample_rate)
            else:
                self.sample_rate = rate

            # One sample is 2 bytes per channel, and a network chunk can end
            # mid-sample — so whole frames are emitted and the remainder carried.
            # Getting this wrong shifts every later sample by a byte, which is
            # not a glitch but white noise for the rest of the turn.
            frame = 2 * max(1, channels)
            block = max(frame, int(self.sample_rate * _BLOCK_SECONDS) * frame)
            pending = bytearray()
            for raw in response.iter_bytes():
                if stop is not None and stop():
                    return
                pending += raw
                while len(pending) >= block:
                    usable = (len(pending) // frame) * frame
                    chunk, pending = bytes(pending[:usable]), bytearray(pending[usable:])
                    yield _downmix(chunk, channels)
            usable = (len(pending) // frame) * frame
            if usable:
                yield _downmix(bytes(pending[:usable]), channels)

    def synthesize(self, text: str) -> np.ndarray:
        """The whole utterance as float32 mono — for the warmup, the tests and the
        audition tool. Built on `stream_pcm` so there is only ever one request
        shape and one decode to get wrong."""
        data = b"".join(self.stream_pcm(text))
        if not data:
            return np.zeros(1, dtype=np.float32)
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0

    # -- decoding ----------------------------------------------------------- #
    # There is deliberately no `_decode_pcm` any more: the PCM path is decoded
    # once, inside `stream_pcm`, and `synthesize` joins its output. Two copies of
    # "interpret these bytes as audio" is precisely the bug this module's
    # docstring is about — the transcript looks perfect either way.
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
