"""
LiveKit worker — connects the deterministic brain to real phone calls.

Pipeline:  phone -> LiveKit SIP -> this worker
           Silero VAD + hosted turn detector -> streaming STT
                     -> CarrierSalesAgent -> streaming TTS

Speech runs on LiveKit Inference by default: transcription streams in WHILE the
caller talks and the voice streams out as it is generated, both over WebSockets
on the LiveKit credentials the worker already holds. The original OpenRouter path
— Whisper as one HTTP POST per utterance, a voice that generates the whole reply
before its first byte — is kept behind STT_PROVIDER / TTS_PROVIDER = openrouter,
and is still what practice mode uses. The composer that writes each turn runs on
OpenRouter or Anthropic per LLM_PROVIDER, unchanged.

Everything the caller could hear before any model has spoken is rendered at
process start: the greeting (composed once — it is the same every call) and the
dead-air fillers, all in the configured voice, played from memory.

Run:  lanevoice-worker dev      (local)
      lanevoice-worker start    (production)
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as _dt
import logging
import os
import random
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterable, Coroutine
from pathlib import Path
from typing import Any

import numpy as np
from livekit import api as lk_api
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    ModelSettings,
    RoomInputOptions,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    inference,
)
from livekit.agents import tts as lk_tts
from livekit.agents import utils as lk_utils
from livekit.plugins import openai as lk_openai
from livekit.plugins import silero

try:  # StopResponse moved across livekit-agents versions
    from livekit.agents import StopResponse
except ImportError:  # pragma: no cover
    from livekit.agents.llm import StopResponse
try:
    from livekit.agents import DEFAULT_API_CONNECT_OPTIONS
except ImportError:  # pragma: no cover
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from lanevoice import geo, parsing
from lanevoice.conversation import CarrierSalesAgent, is_closing_turn
from lanevoice.conversation.agent import compose_greeting
from lanevoice.datasource import build_repository
from lanevoice.db import Database, Repository
from lanevoice.env import load_env
from lanevoice.logging_config import TRACE_LEVEL, get_logger, setup_logging
from lanevoice.settings import Settings, get_settings
from lanevoice.voice import OpenRouterTTS, StubComposer, build_composer
from lanevoice.voice.tts import speechify

# Runtime setup (kept below imports so linting stays clean). load_env() runs
# before get_settings() so a .env — found by searching upward from the working
# directory, not just in it — populates the environment first.
load_env()
_settings = get_settings()
setup_logging(_settings.log_level)
logger = get_logger("lanevoice.worker")

# Which call a log line belongs to. Two callers can be on the line at once in one
# worker process, and their turns interleave in the log with nothing to tell
# them apart — observed live. Set per job in `entrypoint`; inherited by every
# task and `to_thread` call under it.
_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lanevoice_call_id", default=None)


class _CallIdFilter(logging.Filter):
    """Prefix this deployment's own log lines with the call they belong to."""

    def filter(self, record: logging.LogRecord) -> bool:
        call_id = _CALL_ID.get(None)
        if call_id and record.name.startswith("lanevoice") and not getattr(
                record, "_call_tagged", False):
            record.msg = f"[{call_id}] {record.msg}"
            record._call_tagged = True
        return True


_CALL_ID_FILTER = _CallIdFilter()

_SPEECH_PROVIDERS = ("inference", "openrouter")
_INTERRUPTION_MODES = ("vad", "adaptive")


# --------------------------------------------------------------------------- #
# OpenRouter TTS adapter (TTS_PROVIDER=openrouter): wrap OpenRouterTTS in
# LiveKit's TTS interface
# --------------------------------------------------------------------------- #
class OpenRouterTTSPlugin(lk_tts.TTS):
    def __init__(self):
        self._model = OpenRouterTTS(_settings)
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=self._model.sample_rate, num_channels=1,
        )

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return _TTSStream(self, text, self._model, conn_options=conn_options)


class _TTSStream(lk_tts.ChunkedStream):
    """Pushes audio to the caller as it arrives, not after all of it has.

    The whole utterance used to be synthesised, decoded and only then handed over,
    so the caller heard nothing until the last byte had landed. Raw PCM carries no
    header, so any prefix of it is already playable — `OpenRouterTTS.stream_pcm`
    yields ~80ms blocks off the wire and each one goes straight out, which takes
    the body-transfer time out of the silence the caller sits through (measured
    0.1-0.65s, biggest on a full load pitch).

    It does NOT remove the time the provider spends generating before any byte
    exists, which is the larger half and is a per-REQUEST floor rather than a
    per-second one. `stream_pcm` has the measurements. That floor is also why
    `CarrierAgent.tts_node` sends the whole reply as ONE request on this path:
    the framework's default splits a reply into sentences and synthesises them
    one after another, and each sentence would pay the floor again — audible as
    a hole between sentences. It is the floor TTS_PROVIDER=inference removes.

    `stream_pcm` is a SYNC generator over a sync httpx stream — deliberately, so
    the warmup, the tests and `tools/audition_voices.py` keep working unchanged —
    so it is pumped on a worker thread and the blocks come back through a queue
    the event loop can await.
    """

    def __init__(self, tts, text, model, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        super().__init__(tts=tts, input_text=text, conn_options=conn_options)
        self._model = model

    async def _run(self, output_emitter):
        output_emitter.initialize(
            request_id="tts", sample_rate=self._model.sample_rate,
            num_channels=1, mime_type="audio/pcm",
        )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # Set when this turn is abandoned — a caller interrupting, or the line
        # dropping. Polled between blocks so the HTTP response is closed instead
        # of a thread going on filling a queue nobody will read.
        stop = threading.Event()
        _DONE = object()

        def pump() -> None:
            try:
                for block in self._model.stream_pcm(self.input_text, stop=stop.is_set):
                    loop.call_soon_threadsafe(queue.put_nowait, block)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the loop below
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        pumping = loop.run_in_executor(None, pump)
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, BaseException):
                    raise item
                output_emitter.push(item)
            output_emitter.flush()
        finally:
            # On the happy path the thread has already returned and this is a
            # no-op; on cancellation it is what actually ends the request.
            stop.set()
            await asyncio.shield(pumping)


# --------------------------------------------------------------------------- #
# Speech-to-text
# --------------------------------------------------------------------------- #
# Vocabulary the streaming recogniser is told to expect. These reach the model
# (Deepgram `keyterm`, AssemblyAI `keyterms_prompt`) — unlike the Whisper `prompt`
# on OpenRouter, which the gateway documents as accepted and ignored. Words and
# short phrases only: a keyterm biases recognition toward that spelling, so a
# digit string here would do nothing useful and a sentence would be a
# hallucination waiting to happen on a quiet turn. STT_KEYTERMS in .env appends.
STT_KEYTERMS = (
    # "load" on its own as well as "load number": on the first live calls the
    # bare word came back as "Follow" and "node" in front of a correct load id.
    "load", "MC", "MC number", "USDOT", "DOT number", "load number", "rate con",
    "rate confirmation", "dry van", "reefer", "flatbed", "step deck", "power only",
    "deadhead", "lumper", "detention", "layover", "TONU", "book it", "all in",
    "per mile", "pickup", "delivery", "appointment", "dispatch", "broker",
    "carrier", "Circle Logistics",
)


def _stt_keyterms(settings: Settings) -> list[str]:
    extra = [term.strip() for term in settings.stt_keyterms.split(",") if term.strip()]
    region: list[str] = []
    if settings.office_location.strip():
        region = geo.region_keyterms(settings.office_location, settings.stt_region_keyterms_miles,
                                     settings.stt_region_keyterms_max)
        if region:
            logger.info("regional vocabulary: %d towns within %.0f miles of %s added to the "
                        "recogniser's keyterms (%s ...)", len(region),
                        settings.stt_region_keyterms_miles, settings.office_location,
                        ", ".join(region[:6]))
        else:
            logger.warning("OFFICE_LOCATION %r is not a place the city table knows — no "
                           "regional vocabulary for the recogniser.", settings.office_location)
    return list(dict.fromkeys([*STT_KEYTERMS, *extra, *region]))


def _stt_extra_kwargs(model: str) -> dict[str, Any]:
    """Provider options for the streaming recogniser — chiefly, how it writes numbers.

    The parser downstream (`parsing.py`) was tuned on Whisper, which writes numbers
    as DIGITS. So the recogniser is asked to do the same, and the exact formatting
    mode matters. Measured on phone-band clips with engine noise at 10 dB, ten
    lines carriers actually say:

      assemblyai/universal-streaming, format_turns   10/10 parse — "2450", "611349",
                                                     "2513446"; grouped readings like
                                                     "twenty-five, thirteen, four
                                                     forty-six" come back as 2513446
      deepgram/nova-3, numerals                       9/10 — "25 13 4 46" glues fine;
                                                     but "twenty-four fifty" is
                                                     written "24 50", which no rate
                                                     pattern reads
      deepgram/nova-3, smart_format                  WRONG by 100x on a rate:
                                                     "twenty-four seventy-five" ->
                                                     "$24.75"; and hyphenates a load
                                                     number like a phone number
      deepgram/nova-3, no formatting                 words — "six eleven three forty
                                                     nine" is held as the digits 639

    `filler_words` off on Deepgram: "um" and "uh" are noise to the parser.

    AssemblyAI's end-of-turn eagerness matters as much as its formatting. At its
    defaults it finalised "Looking for load" the instant a caller paused for the
    number, the hosted turn detector took that sentence as complete, and the
    number arrived as a second transcript after the turn had been committed — so
    the agent asked for a number it was being given. The confidence threshold and
    the minimum silence make it hold a final through a short mid-sentence pause;
    its own silence backstop (`max_turn_silence`, 2.4s by default) still ends a
    turn the model is unsure about.
    """
    if model.startswith("deepgram/nova"):
        return {"numerals": True, "filler_words": False}
    if model.startswith("assemblyai/"):
        return {"format_turns": True,
                "end_of_turn_confidence_threshold": 0.5,
                "min_end_of_turn_silence_when_confident": 400}
    return {}


def _write_stt_feed_dump(call_id: str, pcm: bytes, sample_rate: int) -> None:
    """The recogniser's exact input for one call, as a WAV beside the recording
    (STT_FEED_DUMP). Best effort; a failure to write is logged, never raised."""
    import wave
    try:
        dest_dir = Path(_settings.db_path).resolve().parent / "call_recordings"
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{call_id}.stt_feed.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        logger.info("STT feed dump written: %s (%.1fs)", path,
                    len(pcm) / 2 / sample_rate)
    except OSError as exc:
        logger.warning("could not write the STT feed dump for %s: %s", call_id, exc)


def with_comfort_noise(frame: rtc.AudioFrame, rng: np.random.Generator,
                       dbfs: float) -> rtc.AudioFrame:
    """The frame with white noise at `dbfs` (RMS) mixed in — see
    `CarrierAgent.stt_node` for why. `dbfs >= 0` returns the frame untouched."""
    if dbfs >= 0:
        return frame
    samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.int32)
    noise = rng.normal(0.0, 32767 * 10 ** (dbfs / 20), size=samples.shape)
    mixed = np.clip(samples + np.rint(noise).astype(np.int32), -32768, 32767).astype(np.int16)
    return rtc.AudioFrame(data=mixed.tobytes(), sample_rate=frame.sample_rate,
                          num_channels=frame.num_channels,
                          samples_per_channel=frame.samples_per_channel)


def build_stt(settings: Settings):
    """The recogniser for the phone line, per STT_PROVIDER."""
    if settings.stt_on_inference:
        # Streaming over a WebSocket on the LiveKit credentials: the transcript
        # is being written while the caller is still talking, and interim results
        # are what let the hosted turn detector and barge-in act on partial speech.
        return inference.STT(
            settings.stt_inference_model,
            language="en",
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
            extra_kwargs=_stt_extra_kwargs(settings.stt_inference_model),
        )
    # OpenRouter's `/audio/transcriptions` is OpenAI-shaped, so the OpenAI plugin
    # drives it verbatim once it is pointed at the gateway. It is a BATCH model:
    # the framework buffers audio until the VAD closes, then sends one request,
    # and the turn cannot end before the answer comes back (measured 1.0-1.8s).
    # `prompt` is documented by OpenRouter as accepted and IGNORED; kept because
    # it costs nothing and starts working if that changes.
    return lk_openai.STT(
        model=settings.stt_model,
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        language="en",
        prompt=settings.stt_prompt,
    )


# --------------------------------------------------------------------------- #
# Text-to-speech
# --------------------------------------------------------------------------- #
def _inference_tts(settings: Settings) -> inference.TTS:
    return inference.TTS(
        settings.tts_inference_model,
        voice=settings.tts_inference_voice.strip() or None,
        language="en",
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


def build_tts(settings: Settings) -> lk_tts.TTS:
    """The voice for the phone line, per TTS_PROVIDER."""
    if settings.tts_on_inference:
        return _inference_tts(settings)
    return OpenRouterTTSPlugin()


# --------------------------------------------------------------------------- #
# Turn detection: has the caller finished, or only paused?
# --------------------------------------------------------------------------- #
def build_turn_detector(settings: Settings) -> inference.TurnDetector | None:
    """The model that reads the transcript so far and says whether the caller is
    done, or None when TURN_DETECTOR is off.

    Without it the session commits a turn MIN_ENDPOINTING_DELAY after the VAD
    hears silence, whatever the words were, and MAX_ENDPOINTING_DELAY is never
    used — that is how "Looking for" became a whole turn on a live call and the
    load number arrived as the next one. With it, a transcript that reads as
    mid-thought waits out MAX for the rest of the sentence.

    `version="v1"` pins the hosted model. Left to choose, the framework runs
    the local mini model outside dev mode, and a production worker would
    quietly run a weaker detector than the one tested. The framework still falls
    back to the mini model by itself if the hosted one fails, and to a plain MIN
    commit if that fails too — a call never waits on the detector.
    """
    if not settings.turn_detector_enabled:
        return None
    return inference.TurnDetector(
        version="v1",
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


# --------------------------------------------------------------------------- #
# Pre-rendered clips: the greeting and the dead-air fillers
# --------------------------------------------------------------------------- #
# Composing a reply measures ~3.4s on the shipped model (tools/measure_latency.py),
# and a caller sitting in that silence says "hello?" — which, before barge-in was
# tuned, cut off the very reply they were waiting for. These are spoken INSTEAD of
# that silence: synthesized once at worker start with the configured voice, played
# from memory with zero synthesis latency the moment a reply is running late.
#
# They are phatic by design — no facts, no names, no numbers — so they are safe in
# any call state, and they are deliberately kept OUT of the transcript record: the
# transcript feeds the composer's dialogue, and "one sec" is noise there.
FILLER_LINES = (
    "Alright, one sec.",
    "Yeah, give me a second here.",
    "Alright, let me check that.",
    "Hang on one moment for me.",
)

# Spoken when the caller was heard talking but nothing was transcribed — see
# `CarrierAgent.on_user_state_changed`. Phatic like the fillers: no facts, safe in
# any state, kept out of the dialogue record.
REASK_LINE = "Sorry, I didn't catch that — say that one more time for me?"
# How many times per call before the agent stops asking. A recogniser that
# returns nothing three times in a row is not going to start; the rep handoff
# paths in the conversation layer are the right end for that call.
_MAX_REASKS = 3

# A caller who has gone quiet after a question: one nudge, then a goodbye. Both
# pre-rendered, both kept out of the dialogue record. See `_idle_watch`.
STILL_THERE_LINE = "You still there?"
IDLE_CLOSE_LINE = "Alright, I'll let you go — give us a call back whenever you're ready."
# Spoken once the re-asks have run out: the line is up, the VAD hears them, and
# the recogniser keeps returning nothing. Better than the silence it replaces.
HEARING_TROUBLE_LINE = ("I'm having a hard time hearing you on this line — sorry about "
                        "that. A rep will give you a call right back.")

# Spoken when the agent said "transferring you to X" and the SIP transfer then
# failed — the rep's line busy, the trunk refusing the transfer. The caller is
# still on the line with us at that point, and silence would be the worst answer.
TRANSFER_FAILED_LINE = ("Looks like I can't get them on the line right now. A rep will "
                        "call you straight back on this one.")
# How long to let the rep's phone ring before the transfer counts as failed.
_TRANSFER_TIMEOUT = 60.0
# A caller's final transcript that arrived while the agent was still talking is
# treated as the answer to what the agent was saying only if it landed within
# this many seconds of the agent finishing — see `_settle_withheld_transcript`.
# Older than that it was a "yeah" under the pitch, and is dropped.
_WITHHELD_ANSWER_WINDOW = 2.5


# --------------------------------------------------------------------------- #
# Handing the call to a person: a cold SIP transfer (REFER) up the trunk
# --------------------------------------------------------------------------- #
def sip_participant_identity(room) -> str | None:
    """The caller's SIP participant in the room, or None on a non-phone session.

    LiveKit's SIP bridge joins the caller as a participant of kind SIP with an
    identity like `sip_+19303334183`; that identity is what the transfer request
    names. Falls back to the identity prefix in case `kind` is not populated.
    """
    participants = list(getattr(room, "remote_participants", {}).values())
    for participant in participants:
        if getattr(participant, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant.identity
    for participant in participants:
        if str(getattr(participant, "identity", "")).startswith("sip_"):
            return participant.identity
    return None


def transfer_request(room_name: str, participant_identity: str,
                     phone: str) -> lk_api.TransferSIPParticipantRequest:
    """The REFER: LiveKit asks the trunk to re-invite the caller to `phone` and
    drops out of the call. `play_dialtone` gives the caller ringing instead of
    silence while the rep's phone is dialled."""
    return lk_api.TransferSIPParticipantRequest(
        room_name=room_name,
        participant_identity=participant_identity,
        transfer_to=f"tel:{phone}",
        play_dialtone=True,
    )

# (text, 16-bit mono PCM, sample rate). The rate travels with the clip: it is
# whatever the voice answered with, not a setting.
Clip = tuple[str, bytes, int]


def _pcm_frames(pcm: bytes, sample_rate: int):
    """Cached 16-bit mono PCM as the AudioFrame stream `session.say` plays."""

    async def gen():
        step = int(sample_rate * 0.02) * 2          # 20ms of int16 mono
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            if len(chunk) < 2:
                break
            yield rtc.AudioFrame(data=chunk, sample_rate=sample_rate,
                                 num_channels=1,
                                 samples_per_channel=len(chunk) // 2)

    return gen()


def _run_blocking(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine to completion from synchronous code.

    Prewarm is called before the job's event loop exists, so `asyncio.run` is the
    normal case. If some future framework version calls it from inside a running
    loop, the coroutine is run on a private loop in a helper thread instead —
    `asyncio.run` refuses to nest, and the alternative is a worker that dies at
    startup for a reason nobody can see on a call.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    outcome: dict[str, Any] = {}

    def runner() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            outcome["error"] = exc

    thread = threading.Thread(target=runner, name="lanevoice-prerender")
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


async def _render_with_inference(settings: Settings, texts: list[str]) -> list[Clip]:
    """Render `texts` in the configured Inference voice, on a THROWAWAY instance.

    The live TTS built for the session must not be used here: an Inference TTS
    lazily binds an aiohttp session to the event loop it first runs on, and the
    loop this runs on is closed the moment rendering ends. Reusing that instance
    would mean a voice that works at startup and fails on the first real turn.
    """
    clips: list[Clip] = []
    async with lk_utils.http_context.open():
        tts = _inference_tts(settings)
        try:
            for text in texts:
                try:
                    pcm = bytearray()
                    rate = None
                    async with tts.synthesize(speechify(text)) as stream:
                        async for audio in stream:
                            pcm += audio.frame.data.tobytes()
                            rate = audio.frame.sample_rate
                    if pcm and rate:
                        clips.append((text, bytes(pcm), rate))
                    else:
                        logger.warning("clip %r rendered as silence; dropped", text)
                except Exception as exc:  # noqa: BLE001 - degrade, don't die
                    logger.warning("clip %r failed to synthesize (%s)", text, exc)
        finally:
            await tts.aclose()
    return clips


def prerender_clips(settings: Settings, texts: list[str], live_tts: lk_tts.TTS) -> list[Clip]:
    """Every text as a clip in the configured voice, or fewer if some fail.

    A clip that won't render costs the feature — a greeting composed live, a
    reply with no filler in front of it — never the worker: the agent without
    these clips is the agent we had yesterday.
    """
    if isinstance(live_tts, OpenRouterTTSPlugin):
        # The OpenRouter model streams synchronously and is already warm; render
        # on it directly and read the rate it actually answered with.
        clips: list[Clip] = []
        for text in texts:
            try:
                pcm = b"".join(live_tts._model.stream_pcm(text))
                clips.append((text, pcm, live_tts._model.sample_rate))
            except Exception as exc:  # noqa: BLE001 - degrade, don't die
                logger.warning("clip %r failed to synthesize (%s)", text, exc)
        return clips
    try:
        return _run_blocking(_render_with_inference(settings, texts))
    except Exception as exc:  # noqa: BLE001 - degrade, don't die
        logger.warning("could not pre-render clips on %s (%s); the greeting will be "
                       "composed live and replies will have no filler",
                       settings.tts_inference_model, exc)
        return []


# --------------------------------------------------------------------------- #
# How many calls at once
# --------------------------------------------------------------------------- #
def call_load(active_calls: int, max_calls: int) -> float:
    """This worker's load as LiveKit understands it: 0.0 idle, 1.0 full.

    The framework's default is CPU use, which says nothing until the calls are
    already suffering. Calls in progress over the cap is the number a desk would
    actually set — "four lines" — and it lets LiveKit send the fifth caller to
    another worker (or, with none, leave the call unanswered rather than let it
    degrade the four in progress).
    """
    if max_calls <= 0:
        return 0.0
    return min(1.0, active_calls / max_calls)


def full_threshold(max_calls: int) -> float:
    """The load at which the worker is full: half a call short of the cap, so
    `max_calls` calls read as full and one fewer as still available."""
    return 1.0 - 0.5 / max(1, max_calls)


_was_full = False


def _report_call_load(server) -> float:
    global _was_full
    active = len(getattr(server, "active_jobs", ()))
    load = call_load(active, _settings.max_concurrent_calls)
    full = load >= full_threshold(_settings.max_concurrent_calls)
    if full != _was_full:
        _was_full = full
        if full:
            logger.warning("worker FULL: %d of %d calls in progress — LiveKit will route new "
                           "callers elsewhere until one ends", active,
                           _settings.max_concurrent_calls)
        else:
            logger.info("worker taking calls again: %d of %d in progress", active,
                        _settings.max_concurrent_calls)
    _heartbeat(active)
    return load


# --------------------------------------------------------------------------- #
# Heartbeat: the dashboard's answer to "is the worker up, and on what?"
# --------------------------------------------------------------------------- #
# Twice on 09-09 the worker's console window was closed after a test call and
# nothing anywhere said so until the next call rang out. The framework polls
# `load_fnc` every few seconds in the worker's main process, so that is where a
# heartbeat costs nothing: a row in the audit database, rewritten every
# HEARTBEAT_SECONDS, carrying the build and the turn-taking settings in force.
HEARTBEAT_SECONDS = 30.0
_STARTED_AT = _dt.datetime.now(_dt.UTC).isoformat()
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_last_heartbeat = 0.0
_status_repo: Repository | None = None


def build_hash() -> str:
    """The short git hash of the code this worker runs, or 'unknown'. Read once,
    at import — a deployment without git on the path still starts."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=3, cwd=Path(__file__).resolve().parent)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


_BUILD = build_hash()


def settings_summary(settings: Settings) -> dict[str, Any]:
    """The turn-taking settings a call runs under, for the heartbeat row — the
    ones that decide what the caller hears and when, not the credentials."""
    return {
        "min_endpointing_delay": settings.min_endpointing_delay,
        "max_endpointing_delay": settings.max_endpointing_delay,
        "turn_detector": settings.turn_detector_enabled,
        "interruption_mode": settings.interruption_mode.strip().lower(),
        "min_interruption_duration": settings.min_interruption_duration,
        "min_interruption_words": settings.min_interruption_words,
        "stream_compose": settings.stream_compose,
        "aec_warmup_seconds": settings.aec_warmup_seconds,
        "filler_delay": settings.filler_delay,
        "filler_min_gap_seconds": settings.filler_min_gap_seconds,
        "llm": settings.resolved_llm_model,
        "stt": settings.stt_inference_model if settings.stt_on_inference else settings.stt_model,
        "tts": settings.tts_inference_model if settings.tts_on_inference else settings.tts_model,
        "max_concurrent_calls": settings.max_concurrent_calls,
    }


def write_heartbeat(active: int, settings: Settings, *, repo: Repository | None = None) -> None:
    """One heartbeat row, now. Best effort: a locked or missing database costs
    the dashboard a heartbeat, never the worker a call."""
    global _status_repo
    try:
        if repo is None:
            if _status_repo is None:
                _status_repo = Repository(Database(settings.db_path))
            repo = _status_repo
        repo.record_worker_status(_WORKER_ID, started_at=_STARTED_AT, build=_BUILD,
                                  calls_live=active, settings=settings_summary(settings))
    except Exception as exc:  # noqa: BLE001 - the heartbeat must never take the worker down
        logger.debug("heartbeat not written: %s", exc)


def _heartbeat(active: int) -> None:
    global _last_heartbeat
    now = time.monotonic()
    if now - _last_heartbeat < HEARTBEAT_SECONDS:
        return
    _last_heartbeat = now
    write_heartbeat(active, _settings)


# --------------------------------------------------------------------------- #
# Call recording
# --------------------------------------------------------------------------- #
def save_call_recording(session_dir: Path, call_id: str,
                        db_path: str | Path, *, warn: bool = True) -> Path | None:
    """Copy the session recorder's finished file out of the job's temp dir.

    livekit-agents records to `<session_dir>/audio.ogg` and DELETES that whole
    directory when the job cleans up — the copy is what makes the call
    replayable from the dashboard. Runs in a shutdown callback, which the
    framework guarantees is after the recorder finalized the file and before
    the temp dir is removed. Best-effort like everything else in shutdown: a
    failed copy costs the replay, never the audit trail.

    Observed live: with two calls in one worker process, the second call's
    recording was not saved and nothing was logged, because a missing file
    returned None in silence. Now any `.ogg` in the directory is taken, and a
    directory with none is logged with its contents, so the next occurrence is
    diagnosable. `warn=False` for the retries that precede the final attempt.
    """
    folder = Path(session_dir)
    source = folder / "audio.ogg"
    if not source.is_file():
        candidates = sorted(folder.glob("*.ogg"), key=lambda p: p.stat().st_mtime) \
            if folder.is_dir() else []
        if candidates:
            source = candidates[-1]
            logger.info("recording for call %s found as %s rather than audio.ogg",
                        call_id, source.name)
        else:
            if warn:
                contents = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else "no dir"
                logger.warning("no recording file for call %s in %s (contents: %s) — the "
                               "recorder had not written one when the job shut down",
                               call_id, folder, contents)
            return None
    try:
        dest_dir = Path(db_path).parent / "call_recordings"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{call_id}.ogg"
        shutil.copyfile(source, dest)
        return dest
    except OSError as exc:
        logger.warning("could not save recording for call %s: %s", call_id, exc)
        return None


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #
def prewarm(proc):
    # Transport Pro or the offline seed data, per DATA_SOURCE. Built once per
    # worker process and shared by every call it handles — the repository caches
    # reads briefly and handles its own concurrency.
    proc.userdata["repo"] = build_repository(_settings)
    # Computed once per process (it reads the city table), logged once.
    proc.userdata["keyterms"] = _stt_keyterms(_settings)
    if _settings.log_level.upper() == "TRACE":
        # The CLI's dev mode sets the framework's loggers to DEBUG after our
        # logging is configured; the trace level has to be re-applied here,
        # inside the worker, to survive that.
        logging.getLogger("livekit.agents").setLevel(TRACE_LEVEL)
        logger.info("livekit.agents at TRACE: transcript hold/flush decisions will be logged")
    # VAD sensitivity is a real tradeoff (noise-immune vs. hearing a short
    # "sure"), so it lives in settings — see the comment there for which way to
    # turn it and why. Still needed with a streaming STT: it is what anchors the
    # end-of-turn clock and detects a caller talking over the agent.
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=_settings.vad_activation_threshold,
        min_speech_duration=_settings.vad_min_speech_duration,
    )
    proc.userdata["stt"] = build_stt(_settings)
    logger.info("stt: %s / %s", _settings.stt_provider,
                _settings.stt_inference_model if _settings.stt_on_inference
                else _settings.stt_model)
    proc.userdata["tts"] = build_tts(_settings)
    logger.info("tts: %s / %s", _settings.tts_provider,
                f"{_settings.tts_inference_model} voice {_settings.tts_inference_voice}"
                if _settings.tts_on_inference
                else f"{_settings.tts_model} voice {_settings.tts_voice}")
    # What decides that the caller has finished — see `build_turn_detector`.
    proc.userdata["turn_detector"] = build_turn_detector(_settings)
    if proc.userdata["turn_detector"] is not None:
        logger.info("turn detector: livekit %s (hosted) — a mid-thought transcript waits "
                    "up to %.1fs for the rest of the sentence, a finished one %.1fs",
                    proc.userdata["turn_detector"].model,
                    _settings.max_endpointing_delay, _settings.min_endpointing_delay)
    else:
        logger.info("turn detector: OFF (TURN_DETECTOR=0) — every turn commits %.1fs after "
                    "silence, whatever the words were", _settings.min_endpointing_delay)
    logger.info("barge-in: %s mode — %.1fs of caller speech and %d word%s cut the agent off; "
                "echo warm-up %s; fillers at most every %.0fs",
                _settings.interruption_mode.strip().lower(),
                _settings.min_interruption_duration, _settings.min_interruption_words,
                "" if _settings.min_interruption_words == 1 else "s",
                f"{_settings.aec_warmup_seconds:.1f}s" if _settings.aec_warmup_seconds > 0
                else "off", _settings.filler_min_gap_seconds)

    # The agent has no scripted lines, so the composer is what lets it talk at all.
    # `build_composer` picks the provider from LLM_PROVIDER and falls back to the
    # offline stub when USE_LLM is off or the provider's key is missing.
    composer = build_composer(_settings)
    proc.userdata["composer"] = composer
    logger.info("composer: %s / %s", _settings.llm_provider,
                _settings.resolved_llm_model)

    # The greeting is the same on every call, so it is composed ONCE here, with
    # nobody waiting, and rendered to audio along with the fillers. Before this
    # the caller sat through an LLM round trip AND a synthesis — 4-5 seconds of
    # silence after pickup — and the composing call blocked the event loop while
    # it did it. The stub composer cannot write a greeting worth rendering.
    greeting_text = None
    if not isinstance(composer, StubComposer):
        greeting_text = compose_greeting(composer, _settings)
        if greeting_text:
            logger.info("greeting composed for this process: %s", greeting_text)
        else:
            logger.warning("greeting could not be pre-composed; it will be composed "
                           "live on each call")

    # Filler clips ride the same voice, so the acknowledgment and the reply
    # sound like one person. Rendered here, at process start, so playing one
    # mid-call costs nothing.
    texts: list[str] = []
    if greeting_text:
        texts.append(greeting_text)
    if _settings.filler_delay > 0:
        texts.extend(FILLER_LINES)
    if _settings.unheard_reask_delay > 0:
        texts.append(REASK_LINE)
        texts.append(HEARING_TROUBLE_LINE)
    if _settings.idle_prompt_seconds > 0:
        texts.extend((STILL_THERE_LINE, IDLE_CLOSE_LINE))
    clips = {text: (pcm, rate) for text, pcm, rate in
             prerender_clips(_settings, texts, proc.userdata["tts"])} if texts else {}
    proc.userdata["greeting"] = (
        (greeting_text, *clips[greeting_text])
        if greeting_text and greeting_text in clips else None)
    proc.userdata["fillers"] = [
        (text, *clips[text]) for text in FILLER_LINES if text in clips]
    proc.userdata["reask"] = (
        (REASK_LINE, *clips[REASK_LINE]) if REASK_LINE in clips else None)
    for key, line in (("hearing_trouble", HEARING_TROUBLE_LINE),
                      ("still_there", STILL_THERE_LINE), ("idle_close", IDLE_CLOSE_LINE)):
        proc.userdata[key] = (line, *clips[line]) if line in clips else None
    for handler in logging.getLogger().handlers:
        if _CALL_ID_FILTER not in handler.filters:
            handler.addFilter(_CALL_ID_FILTER)
    if proc.userdata["greeting"]:
        logger.info("greeting rendered: %.1fs of audio, ready before the phone rings",
                    len(proc.userdata["greeting"][1]) / 2 / proc.userdata["greeting"][2])
    if _settings.filler_delay > 0:
        logger.info("dead-air fillers ready: %d clips (spoken when a reply "
                    "takes > %.1fs)", len(proc.userdata["fillers"]),
                    _settings.filler_delay)

    # Background-noise / echo removal tuned for 8 kHz phone audio. Optional:
    # if the native lib isn't available on this host, carry on without it.
    proc.userdata["noise_cancellation"] = None
    try:
        from livekit.plugins import noise_cancellation
        proc.userdata["noise_cancellation"] = noise_cancellation.BVCTelephony()
        logger.info("noise cancellation: BVCTelephony enabled")
    except Exception as e:  # noqa: BLE001
        logger.warning("noise cancellation unavailable (%s); continuing without", e)


def _interrupted(speech) -> bool:
    """Whether the caller cut a line off — one speech handle or a list of them."""
    speeches = speech if isinstance(speech, list) else [speech]
    return any(getattr(s, "interrupted", False) for s in speeches)


class _TurnSpeech:
    """The bridge from the brain's thread to the caller's ear for one turn.

    The brain composes on a worker thread and, when a turn can be streamed,
    hands over each sentence as the model finishes it (`chunk`) and `end` when
    that line is done. On the event loop the first sentence starts a
    `session.say()` fed by an async iterator, and the sentences that follow go
    into the same utterance — so the voice is already speaking while the model
    is still writing. Measured before this existed: composing was 2.7-5.0s per
    turn and the caller heard nothing until the last word of it.

    A brain that never streams — a money turn, the stub composer, STREAM_COMPOSE
    off — produces no chunks, `first_text` resolves False when the turn is done,
    and the worker says the finished reply as one piece, exactly as before.
    """

    def __init__(self, session, loop: asyncio.AbstractEventLoop):
        self._session = session
        self._loop = loop
        self._items: asyncio.Queue = asyncio.Queue()
        self._current: asyncio.Queue | None = None
        self._closed = False
        self.speeches: list = []
        # True the moment a sentence is on its way to the voice; False when the
        # turn finished without one. The filler waits on this, not on the reply.
        self.first_text: asyncio.Future = loop.create_future()
        self.sentences = 0

    # -- the brain's side: any thread ---------------------------------------- #
    def begin(self) -> None:
        pass                               # nothing to do until the first sentence

    def chunk(self, text: str) -> None:
        self._loop.call_soon_threadsafe(self._items.put_nowait, ("chunk", text))

    def end(self) -> None:
        self._loop.call_soon_threadsafe(self._items.put_nowait, ("end", None))

    # -- the loop's side ------------------------------------------------------ #
    def finished(self) -> None:
        """The brain has returned; nothing more is coming for this turn."""
        self._items.put_nowait(("done", None))

    async def pump(self) -> None:
        while True:
            kind, text = await self._items.get()
            if kind == "chunk":
                if self._current is None and not self._closed:
                    queue: asyncio.Queue = asyncio.Queue()
                    try:
                        self.speeches.append(self._session.say(self._sentences(queue)))
                        self._current = queue
                    except RuntimeError:  # session closing: the rest is unspoken
                        self._closed = True
                if self._current is not None:
                    self._current.put_nowait(text)
                    self.sentences += 1
                if not self.first_text.done():
                    self.first_text.set_result(True)
            elif kind == "end":
                if self._current is not None:
                    self._current.put_nowait(None)
                    self._current = None
            else:                          # done
                if self._current is not None:
                    self._current.put_nowait(None)
                    self._current = None
                if not self.first_text.done():
                    self.first_text.set_result(False)
                return

    @staticmethod
    async def _sentences(queue: asyncio.Queue) -> AsyncIterable[str]:
        while (text := await queue.get()) is not None:
            yield text + " "


class CarrierAgent(Agent):
    def __init__(self, repo: Repository, composer, tts: lk_tts.TTS,
                 fillers: list[Clip] | None = None,
                 greeting: Clip | None = None,
                 reask: Clip | None = None,
                 ctx: JobContext | None = None,
                 hearing_trouble: Clip | None = None,
                 still_there: Clip | None = None,
                 idle_close: Clip | None = None):
        super().__init__(instructions="Carrier sales agent (logic in conversation layer).")
        self.brain = CarrierSalesAgent(repo, composer, _settings)
        self._ctx = ctx                    # the room and the LiveKit API, for transfers
        self._tts = tts
        self._fillers = list(fillers or [])
        self._greeting = greeting
        self._reask = reask
        self._hearing_trouble = hearing_trouble
        self._still_there = still_there
        self._idle_close = idle_close
        self._idle_task: asyncio.Task | None = None
        self._hung_up = False
        self._last_filler: int | None = None
        # The unheard-speech watchdog — see `on_user_state_changed`.
        self._turn_seq = 0                 # bumps on every committed caller turn
        self._turn_in_flight = False       # a reply is being composed or spoken
        self._caller_spoke_while_idle = False
        self._unheard_task: asyncio.Task | None = None
        self._reasks = 0
        self._last_stt: str | None = None  # last text the recogniser produced this turn
        # The last FINAL the recogniser produced since the previous committed
        # turn, with when it arrived — see `_commit_withheld_answer`.
        self._pending_final: tuple[str, float] | None = None
        # When the last dead-air filler was played — see `_filler_due`.
        self._last_filler_at: float | None = None
        # The voice's measurements for the reply in flight — see `on_metrics_collected`.
        self._voice_metrics: list[tuple[float, float, bool]] = []

    # -- the framework's measurements ---------------------------------------- #
    def on_metrics_collected(self, ev) -> None:
        """Log every measurement as before, and keep the voice's for the reply
        in flight so they can be written beside that line of the transcript."""
        _log_metrics(ev)
        metrics = ev.metrics
        if getattr(metrics, "type", "") == "tts_metrics" and self._turn_in_flight:
            self._voice_metrics.append((float(metrics.ttfb or 0.0),
                                        float(metrics.audio_duration or 0.0),
                                        bool(metrics.cancelled)))

    def _turn_voice(self) -> dict[str, Any]:
        """The reply's voice numbers: first audio of its first piece, how long it
        spoke in all, whether any piece was cut."""
        if not self._voice_metrics:
            return {}
        return {"ttfb": round(self._voice_metrics[0][0], 2),
                "speech": round(sum(d for _t, d, _c in self._voice_metrics), 1),
                "cut": True if any(c for _t, _d, c in self._voice_metrics) else None}

    # -- what the recogniser produced, before any filtering ------------------ #
    def on_user_input_transcribed(self, ev) -> None:
        """Every interim and final the recogniser produced, before any filtering —
        the trail for the words that go missing between the caller's mouth and
        `CALLER said`. Observed live: 299953 reached the brain as "93", and six
        caller utterances on one call produced no committed turn at all. Finals
        are logged at INFO (one line per turn); interims at DEBUG."""
        if ev.transcript:
            self._last_stt = ev.transcript
        if ev.is_final:
            logger.info("STT final → %r", ev.transcript)
            if ev.transcript.strip():
                self._pending_final = (ev.transcript, time.monotonic())
        else:
            logger.debug("STT interim → %r", ev.transcript)

    # -- speech that never became a transcript ----------------------------- #
    def on_user_state_changed(self, ev) -> None:
        """Never leave a caller in silence after they have spoken.

        The VAD hears the caller (user state "speaking" then "listening"), the
        recogniser returns nothing — an empty final for a short, quiet "10 AM" is
        what did it on the first live call — and so no turn is ever committed and
        nothing in the pipeline says a word. Four times in a row, then the caller
        hung up. So the end of caller speech arms a timer; if no turn has been
        committed by the time it fires, the agent asks them to say it again.

        Only speech the agent was NOT talking over arms it: a "yeah" under the
        pitch is a backchannel by design, and words over a reply are an
        interruption the framework already handles.
        """
        if ev.new_state == "speaking":
            # The VAD's view of the caller, beside the recogniser's: a "started
            # speaking" with no STT line after it is the caller going unheard.
            logger.info("VAD → caller started speaking")
            self._cancel_idle_watch()
        elif ev.new_state == "listening" and ev.old_state == "speaking":
            logger.info("VAD → caller stopped speaking")
        if _settings.unheard_reask_delay <= 0:
            return
        if ev.new_state == "speaking":
            self._cancel_unheard_watch()
            self._caller_spoke_while_idle = (
                self.session.current_speech is None and not self._turn_in_flight)
        elif ev.new_state == "listening" and ev.old_state == "speaking":
            if self._caller_spoke_while_idle:
                self._unheard_task = asyncio.create_task(
                    self._reask_if_unheard(self._turn_seq))

    def _cancel_unheard_watch(self) -> None:
        if self._unheard_task is not None and not self._unheard_task.done():
            self._unheard_task.cancel()
        self._unheard_task = None

    async def _reask_if_unheard(self, seq: int) -> None:
        try:
            await asyncio.sleep(_settings.unheard_reask_delay)
        except asyncio.CancelledError:
            return
        if (self._turn_seq != seq or self._turn_in_flight
                or self.session.current_speech is not None
                or self.session.user_state == "speaking"):
            return  # a turn landed, a reply is under way, or they are talking again
        # Right after the pitch the only answer that fits is "go ahead": read the
        # requirements rather than ask them to say it again. That re-ask is what
        # made the requirements a no-pause follow-on from 09-03 to 09-09.
        more = await asyncio.to_thread(self.brain.proceed_without_answer)
        if more:
            logger.info("UNHEARD → the caller answered the pitch but nothing was transcribed "
                        "(recogniser's last output %r); taking it as 'go ahead' and reading "
                        "the requirements", self._last_stt)
            await self._speak_line(more)
            return
        if self._reasks >= _MAX_REASKS:
            # The re-asks are spent and they are still not coming through. Say
            # so, promise the callback, put it on the record, and end the call —
            # observed live: the cap was reached and the agent sat mute for forty
            # seconds until the caller gave up.
            logger.info("UNHEARD → still nothing after %d re-asks; telling the caller a "
                        "rep will call back and ending the call", _MAX_REASKS)
            await self._say_clip(self._hearing_trouble, HEARING_TROUBLE_LINE)
            await asyncio.to_thread(self.brain.give_up_unheard, HEARING_TROUBLE_LINE)
            await self._hang_up()
            return
        self._reasks += 1
        logger.info("UNHEARD → the caller spoke but no turn arrived in %.1fs; the "
                    "recogniser's last output since the previous turn was %r; asking "
                    "them to repeat (%d of %d)", _settings.unheard_reask_delay,
                    self._last_stt, self._reasks, _MAX_REASKS)
        await asyncio.to_thread(self.brain.note_unheard)
        await self._say_clip(self._reask, REASK_LINE)
        self._arm_idle_watch()

    async def _speak_line(self, text: str) -> None:
        """A brain line spoken outside a caller turn — the requirements, when the
        caller's go-ahead was heard but not transcribed. Marked in flight so the
        watchdogs stand down while it plays; the idle watch is re-armed after."""
        self._turn_in_flight = True
        try:
            logger.info("AGENT reply → %s", text)
            await self._speech_finished(self.session.say(text), text)
        except RuntimeError as e:
            logger.info("Could not speak (session closing): %s", e)
        finally:
            self._turn_in_flight = False
        if self.brain.state.value == "done":
            await self._hang_up()
        else:
            self._arm_idle_watch()

    async def _say_clip(self, clip: Clip | None, text: str) -> None:
        """A pre-rendered line, or the same words through the voice when the clip
        is missing. Never part of the dialogue record; never raises."""
        try:
            if clip is not None:
                _text, pcm, rate = clip
                await self.session.say(text, audio=_pcm_frames(pcm, rate),
                                       add_to_chat_ctx=False)
            else:
                await self.session.say(text, add_to_chat_ctx=False)
        except RuntimeError:
            pass                           # session closing

    # -- a caller who has gone quiet ---------------------------------------- #
    def _arm_idle_watch(self) -> None:
        """Start the clock on the caller's next words. Armed whenever the agent
        has just finished speaking and is waiting; cancelled the moment the VAD
        hears them."""
        self._cancel_idle_watch()
        if _settings.idle_prompt_seconds <= 0 or self.brain.state.value == "done":
            return
        self._idle_task = asyncio.create_task(self._idle_watch(self._turn_seq))

    def _cancel_idle_watch(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _caller_is_back(self, seq: int) -> bool:
        return (self._turn_seq != seq or self._turn_in_flight
                or self.session.current_speech is not None
                or self.session.user_state == "speaking")

    async def _idle_watch(self, seq: int) -> None:
        """"You still there?" after the first silence; goodbye after the second.

        A real caller who set the phone down, walked off or lost the line
        otherwise holds the call open indefinitely — every minute billed, and a
        desk that never noticed. Both lines are pre-rendered and stay out of the
        dialogue; the close is recorded as abandoned.
        """
        try:
            await asyncio.sleep(_settings.idle_prompt_seconds)
            if self._caller_is_back(seq):
                return
            logger.info("IDLE → nothing from the caller for %.0fs; asking if they're there",
                        _settings.idle_prompt_seconds)
            await self._say_clip(self._still_there, STILL_THERE_LINE)
            await self._settle_withheld_transcript(more_coming=False)
            await asyncio.sleep(_settings.idle_close_seconds)
            if self._caller_is_back(seq):
                return
            logger.info("IDLE → still nothing after the prompt; closing the call")
            await self._say_clip(self._idle_close, IDLE_CLOSE_LINE)
            await asyncio.to_thread(self.brain.close_idle, IDLE_CLOSE_LINE)
            await self._hang_up()
        except asyncio.CancelledError:
            return

    # -- ending the call ---------------------------------------------------- #
    async def _hang_up(self) -> None:
        """End the call once the closing line has been heard.

        A desk hangs up after "have a good one". Until this existed the agent
        said goodbye and the line stayed open — the caller left wondering, the
        minutes still billing — and a handoff the trunk cannot perform ended in
        the same open silence. Deleting the room drops the SIP leg; the job's
        shutdown then finalizes the record and the recording as for any hang-up.
        """
        if self._hung_up or self._ctx is None or _settings.hangup_after_close_seconds <= 0:
            return
        self._hung_up = True
        self._cancel_idle_watch()
        self._cancel_unheard_watch()
        await asyncio.sleep(_settings.hangup_after_close_seconds)
        outcome = self.brain.outcome.value if self.brain.outcome else "open"
        logger.info("HANGUP → the call is finished (%s); ending the room", outcome)
        try:
            await self._ctx.delete_room()
        except Exception as exc:  # noqa: BLE001 - the caller can still hang up themselves
            logger.warning("could not end the room: %s", exc)

    async def _compose_follow_on(self, after) -> tuple[str, Any] | None:
        """Compose the second half of the agent's turn WHILE the first half plays,
        and queue it to play straight after — the load's requirements right
        behind the load. See `CarrierSalesAgent.continue_turn`.

        It used to be composed only after the first half had finished, which
        left three to five seconds of silence between "got a couple of
        requirements to run through" and the requirements. Observed live on
        09-09: the caller filled that silence with "Okay.", the framework took
        the words as a barge-in on the line that was about to start, the
        requirements were never heard, and "Okay." was then read as agreeing to
        them. Queued behind the first half there is no silence to fill.

        Returns (text, handle); (text, None) when the first half was cut off
        before this was ready — composed, never spoken; None when there was
        nothing to add.
        """
        more = await asyncio.to_thread(self.brain.continue_turn)
        if not more:
            return None
        if _interrupted(after):
            logger.info("FOLLOW-ON withdrawn: the caller cut in while it was being composed "
                        "→ %s", more)
            return more, None
        logger.info("AGENT reply (continued) → %s", more)
        try:
            return more, self.session.say(more)
        except RuntimeError:
            return more, None              # session closing

    @staticmethod
    def _heard_text(speech) -> str:
        """What the caller actually heard of a line: the framework records the
        text aligned to the audio that played, and nothing when none did."""
        return " ".join(
            (getattr(item, "text_content", None) or "")
            for item in (getattr(speech, "chat_items", None) or [])).strip()

    async def _speech_finished(self, speech, text: str, *, more_coming: bool = False) -> bool:
        """Wait out one of our lines; True if the caller cut it off.

        Barge-in cuts our audio mid-word. The transcript records what was
        composed, so the brain is told what was HEARD — nothing, when the cut
        came before the first word — and it corrects the record and, for the
        requirements or the pitch, its own state (see
        `CarrierSalesAgent.note_playback_cut`). A line that played to the end
        then settles any short caller transcript the framework held back while
        it was playing.
        """
        speeches = speech if isinstance(speech, list) else [speech]
        for one in speeches:
            await one
        if _interrupted(speeches):
            heard = " ".join(h for h in (self._heard_text(s) for s in speeches) if h)
            logger.info("PLAYBACK CUT by caller → %s (heard: %s)", text,
                        repr(heard) if heard else "none of it")
            await asyncio.to_thread(self.brain.note_playback_cut, text, heard)
            return True
        await self._settle_withheld_transcript(more_coming=more_coming)
        return False

    async def _after_reply(self, speech, reply: str) -> None:
        """Everything that follows the reply to a caller's turn.

        The follow-on (the requirements after the load) is composed while the
        reply plays and queued to play right behind it. If the caller cuts the
        reply, the follow-on is withdrawn: whatever they said is their next
        turn, the framework holds it until this handler returns, and reading
        the follow-on first would mean a caller who asked "what's it paying?"
        hears the whole requirements list and then the answer. The brain is
        told the line was never heard, so it is read on the next turn. The
        handoff is dialled whether or not they cut in — they asked for a person.
        """
        speeches = speech if isinstance(speech, list) else [speech]
        follow = (asyncio.create_task(self._compose_follow_on(speeches))
                  if self.brain.pending_followup else None)
        for one in speeches:
            await one
        # The brain is single-threaded by convention: let the follow-on finish
        # composing before the cut is written into its record.
        queued = await follow if follow is not None else None
        interrupted = await self._speech_finished(speeches, reply, more_coming=queued is not None)
        await self._transfer_if_pending()
        if queued is None:
            return
        more, handle = queued
        if handle is None:                       # composed after the cut; never spoken
            await asyncio.to_thread(self.brain.note_playback_cut, more, "")
            await asyncio.to_thread(self.brain.record_event, "followon_withdrawn",
                                    "the caller cut in; the second half was never spoken")
            return
        if interrupted:
            handle.interrupt()                   # queued behind a line they cut: withdraw it
            await asyncio.to_thread(self.brain.record_event, "followon_withdrawn",
                                    "the caller cut in; the queued second half was withdrawn")
        await self._speech_finished(handle, more)

    async def _settle_withheld_transcript(self, *, more_coming: bool) -> None:
        """Deal with a short caller transcript the framework held while we spoke.

        MIN_INTERRUPTION_WORDS has a second effect inside the framework: a final
        transcript under that many words is not COMMITTED as a turn while the
        agent's audio is still playing — it is held and glued to whatever the
        caller says next. Two live failures came out of that:

        * "Yes." given as we finished asking "can you handle both of those?" was
          held, the question stood unanswered, the unheard watchdog did not arm
          (a speech was still active), and the caller repeated themselves
          twelve seconds later — "Yes. Yes." on the 09-04 call.
        * A one-word "9." heard fourteen seconds into the pitch was glued onto
          the caller's later "Okay." — two words, so it counted as a barge-in on
          the line that was about to play, and that line was never heard (09-09).

        So once our audio has finished and the caller is quiet: a final that
        arrived in the last stretch of our line, with nothing more of ours
        queued behind it, is committed as their turn. Anything older — or
        anything said under the first half of a two-part turn — was a
        backchannel and is dropped, so it cannot inflate or corrupt their next
        words.
        """
        pending = self._pending_final
        if pending is None or _settings.min_interruption_words <= 0:
            return
        if self.session.user_state == "speaking":
            return                         # they are talking; the normal path owns it
        text, heard_at = pending
        age = time.monotonic() - heard_at
        if age <= _WITHHELD_ANSWER_WINDOW and not more_coming:
            # `say()` resolves a moment before the framework releases the speech
            # handle, and until it has, the commit would be refused as a barge-in.
            for _ in range(10):
                if self.session.current_speech is None:
                    break
                await asyncio.sleep(0.05)
            if self.session.current_speech is not None or self.session.user_state == "speaking":
                return
            self._pending_final = None
            logger.info("WITHHELD → %r arrived while we were speaking; committing it as the "
                        "caller's turn", text)
            try:
                self.session.commit_user_turn(transcript_timeout=0.3)
            except RuntimeError:
                pass                       # session closing
            await asyncio.to_thread(
                self.brain.record_event, "withheld_committed",
                f"{text!r} arrived under our last words and was committed as the caller's "
                f"turn once we stopped", age=round(age, 1))
            return
        self._pending_final = None
        logger.info("BACKCHANNEL → %r heard under our line %.0fs ago; dropped so it is not "
                    "glued onto their next words", text, age)
        try:
            self.session.clear_user_turn()
        except RuntimeError:
            pass                           # session closing
        await asyncio.to_thread(
            self.brain.record_event, "backchannel_dropped",
            f"{text!r} heard under our line {age:.0f}s earlier was dropped", age=round(age, 1))

    def _filler_due(self) -> bool:
        """Whether a filler may play on this turn — see FILLER_MIN_GAP_SECONDS."""
        gap = _settings.filler_min_gap_seconds
        if gap <= 0 or self._last_filler_at is None:
            return True
        return time.monotonic() - self._last_filler_at >= gap

    def _next_filler(self) -> Clip:
        """A filler that isn't the one just used — the same 'one sec' twice in a
        row is what makes a caller notice it's canned."""
        choices = [i for i in range(len(self._fillers)) if i != self._last_filler]
        self._last_filler = random.choice(choices or [0])
        return self._fillers[self._last_filler]

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame],
                       model_settings: ModelSettings):
        """Keep the recogniser awake through the caller's silences.

        Between the caller's utterances the phone audio, after noise cancellation,
        is digital zero — and AssemblyAI's streaming speech detector goes idle on
        it. After 15-20s of that it needs the first ~0.3-0.5s of the next
        utterance to wake up, and that audio is lost: "Transfer it to the person"
        reached the agent as "Sfer it to the person", an MC said as "299953" came
        back as "93", and a one-second answer produced nothing at all — six times
        on one call, each met with silence until the caller said "Hello". Reproduced
        offline from the recordings: the same utterance after 20s of digital
        silence is clipped, after 3s it is whole, and after 20s of white noise at
        -55 dBFS it is whole. (Deepgram clipped the same clip harder.) So a whisper
        of noise, 35 dB under quiet speech, rides every frame into the recogniser
        and nowhere else: the VAD, the recorder and the caller never hear it.
        STT_COMFORT_NOISE_DBFS=0 turns it off.
        """
        dbfs = _settings.stt_comfort_noise_dbfs
        logger.info("STT feed: comfort noise %s", f"{dbfs:.0f} dBFS" if dbfs < 0 else "off")

        async def probed():
            # What the recogniser was HANDED while the VAD heard the caller. A
            # transcript missing its first words with a healthy peak here means
            # the recogniser dropped them; digital-zero frames here mean the
            # framework substituted silence before this point. Observed live:
            # "Can you transfer me to a person?" whole in the recording, "To a
            # person." from the recogniser, 1.5s after the agent stopped talking.
            rng = np.random.default_rng()
            speaking = False
            frames = zeros = 0
            peak = 0
            seconds = 0.0
            # The second BEFORE the VAD flipped: the first syllable of an answer
            # lands here (the VAD needs ~0.2s to decide), so a clipped leading
            # word is judged by this window, not by the speaking stretch.
            recent: list[tuple[float, int]] = []      # (duration, peak) per frame
            dump = bytearray() if _settings.stt_feed_dump else None
            dump_rate = 0
            try:
                async for frame in audio:
                    try:
                        now_speaking = self.session.user_state == "speaking"
                    except RuntimeError:          # no session yet
                        now_speaking = False
                    samples = np.frombuffer(frame.data, dtype=np.int16)
                    top = int(np.abs(samples).max()) if samples.size else 0
                    if now_speaking:
                        if not speaking:
                            speaking, frames, zeros, peak, seconds = True, 0, 0, 0, 0.0
                            before = max((p for _d, p in recent), default=0)
                            logger.info("STT feed in the second before the VAD flipped → peak %s",
                                        f"{20 * np.log10(before / 32767):.0f} dBFS" if before
                                        else "digital silence")
                        frames += 1
                        seconds += frame.duration
                        zeros += top == 0
                        peak = max(peak, top)
                    elif speaking:
                        speaking = False
                        level = f"{20 * np.log10(peak / 32767):.0f} dBFS" if peak else "silence"
                        logger.info("STT feed during caller speech → %.1fs of audio, peak %s, "
                                    "%d of %d frames digital zero", seconds, level, zeros, frames)
                    recent.append((frame.duration, top))
                    while recent and sum(d for d, _p in recent) > 1.0 + frame.duration:
                        recent.pop(0)
                    out = with_comfort_noise(frame, rng, dbfs) if dbfs < 0 else frame
                    if dump is not None:
                        dump += bytes(out.data)
                        dump_rate = out.sample_rate
                    yield out
            finally:
                if dump and dump_rate:
                    _write_stt_feed_dump(self.brain.call_id, bytes(dump), dump_rate)

        async for event in Agent.default.stt_node(self, probed(), model_settings):
            yield event

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        """Every composed line passes through here on its way to the voice.

        `speechify` rewrites money and identifiers into words the voice says
        correctly — "$2450" as "two thousand, four hundred and fifty dollars",
        "L1002" as "L 1 0 0 2". The OpenRouter model does that inside its own
        request body; the Inference voice has no such hook, so it happens here.

        On the OpenRouter path the reply also goes to the provider as ONE request.
        The framework's default node would split it into sentences and synthesise
        them one after another, the next request starting only when the previous
        sentence's bytes have all arrived — and with that provider's 1-2s
        generation floor a short opener ("Alright.") left a hole before the next
        sentence. One request, streamed as it arrives, is what `_TTSStream` was
        built for.
        """
        if isinstance(self._tts, OpenRouterTTSPlugin):
            whole = "".join([chunk async for chunk in text])
            if not whole.strip():
                return
            async with self._tts.synthesize(whole) as stream:
                async for audio in stream:
                    yield audio.frame
            return

        async def spoken() -> AsyncIterable[str]:
            async for chunk in text:
                yield speechify(chunk)

        async for frame in Agent.default.tts_node(self, spoken(), model_settings):
            yield frame

    # -- handing the call to a person ---------------------------------------- #
    async def _transfer_if_pending(self) -> None:
        """Dial the rep the brain resolved, once the handoff line has been spoken.

        The brain sets `pending_transfer` only for a rep with a number — the load's
        carrier sales rep from Transport Pro, or the desk's own directory entry
        for them. The transfer is a SIP REFER: the trunk re-invites the caller to
        the rep's number and this session ends when they leave the room. What
        happened is written back to the call record either way, and a failure is
        SAID to the caller, who is otherwise sitting in silence after being told
        they were being put through.
        """
        rep = self.brain.pending_transfer
        if rep is None:
            return
        self.brain.pending_transfer = None
        if not _settings.sip_transfer_enabled:
            # Announced, resolved, not dialled: the trunk isn't set up for it yet.
            logger.info("TRANSFER not performed (SIP_TRANSFER_ENABLED is off): would "
                        "dial %s at %s", rep.name, rep.phone)
            await asyncio.to_thread(self.brain.note_transfer_skipped, rep)
            return
        room = self._ctx.room if self._ctx is not None else None
        identity = sip_participant_identity(room) if room is not None else None
        if identity is None:
            await self._transfer_failed(rep, "no SIP participant in the room — not a "
                                             "phone call, or the caller already left")
            return
        logger.info("TRANSFER → %s at %s (SIP REFER for %s)", rep.name, rep.phone, identity)
        try:
            await asyncio.wait_for(
                self._ctx.api.sip.transfer_sip_participant(
                    transfer_request(room.name, identity, rep.phone)),
                timeout=_TRANSFER_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a failed transfer is told to the caller, never raised
            await self._transfer_failed(rep, f"{type(exc).__name__}: {exc}")
            return
        logger.info("TRANSFER connected → %s", rep.name)
        await asyncio.to_thread(self.brain.note_transfer_result, rep, True)

    async def _transfer_failed(self, rep, detail: str) -> None:
        logger.error("TRANSFER failed → %s at %s: %s", rep.name, rep.phone, detail)
        await asyncio.to_thread(self.brain.note_transfer_result, rep, False, detail)
        try:
            await self.session.say(TRANSFER_FAILED_LINE, add_to_chat_ctx=False)
        except RuntimeError:
            pass                           # session closing; the caller is gone

    async def _acknowledge_if_slow(self, ready: asyncio.Future) -> None:
        """Fill the composing gap with a spoken acknowledgment, never silence.

        Waits FILLER_DELAY for `ready` — the reply's first sentence on its way
        to the voice, or the whole reply when the turn is not streamed. If it
        isn't there, plays a cached clip while composition keeps running in its
        thread. The say() is awaited so the reply queues naturally behind it
        instead of colliding with it.
        """
        if not self._fillers or _settings.filler_delay <= 0 or not self._filler_due():
            return
        done, _ = await asyncio.wait({ready}, timeout=_settings.filler_delay)
        if done:
            return
        text, pcm, rate = self._next_filler()
        self._last_filler_at = time.monotonic()
        try:
            await self.session.say(
                text,
                audio=_pcm_frames(pcm, rate),
                add_to_chat_ctx=False,   # phatic — not part of the record
            )
        except RuntimeError:
            pass                          # session closing; the reply say() will report

    def _log_sip_state(self, when: str) -> None:
        """What LiveKit knows about the caller's leg: the SIP call status, which
        trunk took the call, and whether their audio track is published and
        subscribed. A call that connects with no audio in either direction is
        otherwise invisible from inside the agent."""
        room = self._ctx.room if self._ctx is not None else None
        if room is None:
            return
        for participant in getattr(room, "remote_participants", {}).values():
            attrs = {k: v for k, v in dict(getattr(participant, "attributes", {}) or {}).items()
                     if k.startswith("sip.")}
            tracks = []
            for pub in getattr(participant, "track_publications", {}).values():
                tracks.append(f"{getattr(pub, 'source', '?')}:"
                              f"{'muted' if getattr(pub, 'muted', False) else 'live'}/"
                              f"{'subscribed' if getattr(pub, 'subscribed', False) else 'unsub'}")
            logger.info("SIP %s → %s kind=%s attrs=%s tracks=%s", when, participant.identity,
                        getattr(participant, "kind", "?"), attrs, tracks)

    async def _wait_for_caller_media(self) -> None:
        """Hold the greeting until the caller's leg is actually connected.

        LiveKit adds the SIP participant to the room while the call is still
        RINGING and flips `sip.callStatus` to "active" once media is up. Observed
        live: 'ringing' at pickup, 'active' six seconds later — and the greeting,
        played at once from its pre-rendered clip, went into a leg nobody was
        connected to. Five callers in a row heard silence and hung up.

        Sessions with no SIP participant (the dashboard, tests) return at once. A
        status that never turns active gives up after SIP_MEDIA_WAIT_SECONDS and
        greets anyway, which is what the agent did before this existed.
        """
        room = self._ctx.room if self._ctx is not None else None
        timeout = _settings.sip_media_wait_seconds
        if room is None or timeout <= 0:
            return
        started = time.monotonic()
        statuses: list[str] = []
        while time.monotonic() - started < timeout:
            participants = list(getattr(room, "remote_participants", {}).values())
            statuses = [
                str(status) for status in (
                    dict(getattr(p, "attributes", {}) or {}).get("sip.callStatus")
                    for p in participants)
                if status]
            if participants and not statuses:
                return                     # not a phone call — nothing to wait for
            if statuses and all(status == "active" for status in statuses):
                waited = time.monotonic() - started
                if waited >= 0.3:
                    logger.info("SIP leg active after %.1fs — greeting now", waited)
                    await asyncio.to_thread(
                        self.brain.record_event, "sip_wait",
                        f"the greeting waited {waited:.1f}s for the SIP leg to turn active",
                        seconds=round(waited, 1))
                return
            await asyncio.sleep(0.1)
        logger.warning("SIP leg still %s after %.0fs — greeting anyway",
                       statuses or "absent", timeout)

    async def on_enter(self):
        # Who is calling, from the SIP leg (`sip_+12602649808`): recorded on the
        # call and repeated in the summary note the rep reads on the load.
        identity = sip_participant_identity(self._ctx.room) if self._ctx is not None else None
        if identity:
            number = identity.removeprefix("sip_")
            logger.info("CALLER number → %s", number)
            await asyncio.to_thread(self.brain.set_caller, number)
        self._log_sip_state("at pickup")
        await self._wait_for_caller_media()
        if self._greeting is not None:
            # Composed and rendered at process start: the caller hears a voice the
            # moment the line connects. `greet_with` only records the line, but
            # that record is a SQLite write, so it stays off the event loop.
            greeting, pcm, rate = self._greeting
            await asyncio.to_thread(self.brain.greet_with, greeting)
            logger.info("GREETING → %s (pre-rendered)", greeting)
            speech = self.session.say(greeting, audio=_pcm_frames(pcm, rate))
        else:
            # No clip (a composer or voice failure at startup): compose it live, in
            # a thread. This used to run ON the event loop — an LLM round trip
            # during which nothing else in the session could move.
            greeting = await asyncio.to_thread(self.brain.greeting)
            logger.info("GREETING → %s", greeting)
            speech = self.session.say(greeting)
        await self._speech_finished(speech, greeting)
        self._arm_idle_watch()

    async def on_user_turn_completed(self, turn_ctx, new_message):
        user_text = (getattr(new_message, "text_content", None) or "").strip()
        # Whatever the recogniser produced is in this turn now; nothing is held.
        self._pending_final = None
        # Ignore empty fragments and transcriber hallucinations ("Thank you.",
        # "you", "so"…) so the agent waits for real speech instead of replying to a phantom.
        if len(user_text) < 2 or parsing.is_probably_noise(user_text):
            logger.debug("Ignoring noise/empty transcript: %r", user_text)
            raise StopResponse()
        logger.info("CALLER said → %s", user_text)
        _log_end_of_turn(new_message)
        heard_timing = _heard_timing(new_message, _settings)
        # A real turn landed: the watchdog for unheard speech stands down. (A
        # phantom filtered above does not count — the caller still went unheard.)
        self._turn_seq += 1
        self._last_stt = None
        self._cancel_unheard_watch()
        self._cancel_idle_watch()
        self._turn_in_flight = True
        self._voice_metrics = []
        # The voice starts on the reply's first sentence: the brain hands each
        # one over as the model finishes it, and `turn` feeds them to one
        # utterance while the rest is still being written. See `_TurnSpeech`.
        turn = _TurnSpeech(self.session, asyncio.get_running_loop())
        self.brain.speech_sink = turn if _settings.stream_compose else None
        pump = asyncio.create_task(turn.pump())
        try:
            reply_task = asyncio.create_task(
                asyncio.to_thread(self.brain.handle, user_text, heard_timing))
            reply_task.add_done_callback(lambda _t: turn.finished())
            # Every filler promises work is coming ("Alright, let me check that."),
            # and in front of a goodbye that promise is nonsense — observed live, a
            # caller's "No. Thank you." was answered with a filler and THEN the
            # close. A beat of silence before a goodbye is fine; skip the filler.
            if not is_closing_turn(user_text):
                await self._acknowledge_if_slow(turn.first_text)
            reply = await reply_task
            await pump
            logger.info("AGENT reply → %s%s", reply,
                        f" (streamed, {turn.sentences} sentence"
                        f"{'' if turn.sentences == 1 else 's'})" if turn.speeches else "")
            timing = self.brain.last_turn_timing
            if timing:
                logger.info(
                    "TIMING brain → %.2fs (compose %.2fs over %d call%s; lookups and "
                    "bookkeeping %.2fs) in %s",
                    timing["total"], timing["compose"], timing["compose_calls"],
                    "" if timing["compose_calls"] == 1 else "s", timing["other"],
                    timing["state"])
            try:
                # Streamed: the utterance(s) are already playing. Not streamed
                # (a money turn, the switch off): say the finished reply whole.
                speeches = turn.speeches or [self.session.say(reply)]
                await self._after_reply(speeches, reply)
                if voice := self._turn_voice():
                    await asyncio.to_thread(self.brain.note_turn_voice, **voice)
            except RuntimeError as e:  # e.g. caller hung up mid-turn
                logger.info("Could not speak (session closing): %s", e)
        finally:
            self.brain.speech_sink = None
            self._turn_in_flight = False
            if not pump.done():
                pump.cancel()
        if self.brain.state.value == "done":
            await self._hang_up()
        else:
            self._arm_idle_watch()
        raise StopResponse()   # we answered this turn ourselves; skip the LLM node


def turn_handling(settings: Settings) -> TurnHandlingOptions:
    """The session's turn-taking rules, from settings.

    Endpointing: MIN applies when the hosted turn detector reads the caller as
    finished, MAX when it reads them as mid-thought. Interruption: how much
    continuous speech — and, now that the STT streams interim words, how many of
    them — it takes to cut the agent off, and what happens when an "interruption"
    never turns into words (a cough, a horn: the cut line resumes). Short
    line-checks ("hello?") must not cut the agent's audio; a caller genuinely
    talking over it still should. See the settings comments for each number.
    """
    return {
        "endpointing": {
            "min_delay": settings.min_endpointing_delay,
            "max_delay": settings.max_endpointing_delay,
        },
        "interruption": {
            "enabled": settings.allow_interruptions,
            # Pinned: left out, the framework picks a different strategy in dev
            # and in production — see INTERRUPTION_MODE in settings.
            "mode": settings.interruption_mode.strip().lower(),
            "min_duration": settings.min_interruption_duration,
            "min_words": settings.min_interruption_words,
            "resume_false_interruption": settings.resume_false_interruption,
            "false_interruption_timeout": settings.false_interruption_timeout,
        },
    }


def _log_metrics(ev) -> None:
    """One log line per framework measurement, so a call's latency can be read
    off the log turn by turn.

    end-of-turn: how long after the caller stopped the transcript was in hand, and
    how long after that the turn was declared over (the endpointing wait). voice:
    how long the caller waited for the first audio of a reply. The brain's own
    split (compose vs. lookups) is logged by `CarrierAgent` beside these, so the
    four numbers together are the whole gap the caller sat through.
    """
    metrics = ev.metrics
    kind = getattr(metrics, "type", "")
    if kind == "eou_metrics":
        logger.info("TIMING end-of-turn → transcript %.2fs after the caller stopped, "
                    "turn ended %.2fs after",
                    metrics.transcription_delay, metrics.end_of_utterance_delay)
    elif kind == "tts_metrics":
        logger.info("TIMING voice → first audio %.2fs; %.1fs of speech for %d chars%s",
                    metrics.ttfb, metrics.audio_duration, metrics.characters_count,
                    " (cut off by the caller)" if metrics.cancelled else "")
    elif kind == "eot_inference_metrics":
        logger.debug("TIMING turn detector → %.2fs", metrics.total_duration)
    elif kind == "stt_metrics":
        logger.debug("TIMING stt → %.2fs for %.1fs of audio",
                     metrics.duration, metrics.audio_duration)


def _heard_timing(new_message, settings: Settings) -> dict[str, Any] | None:
    """The caller's wait on this line, as numbers for the transcript's clock:
    how long after they stopped the transcript existed (`stt`), how long after
    they stopped the turn was committed (`eou`), and whether that was the
    maximum — the turn detector reading a finished sentence as unfinished, which
    on the 10:16 call of 09-09 happened on every turn."""
    metrics = getattr(new_message, "metrics", None) or {}
    if "end_of_turn_delay" not in metrics:
        return None
    eou = float(metrics["end_of_turn_delay"])
    out: dict[str, Any] = {"eou": round(eou, 2)}
    if metrics.get("transcription_delay") is not None:
        out["stt"] = round(float(metrics["transcription_delay"]), 2)
    if settings.turn_detector_enabled and eou >= settings.max_endpointing_delay - 0.05:
        out["max"] = True
    return out


def _log_end_of_turn(new_message) -> None:
    """The caller's side of the wait, off the user message the framework hands us.

    `on_user_turn_completed` answers every turn itself and raises StopResponse,
    and on that path the framework neither emits its end-of-turn metrics event
    nor adds the user message to the conversation — but it has already attached
    the numbers to that message before calling us. So they are read here: how
    long after the caller stopped the transcript was in hand, and how long after
    that the turn was declared over (the endpointing wait). Beside the brain and
    voice lines, that is the whole gap the caller sat through.
    """
    metrics = getattr(new_message, "metrics", None) or {}
    if "end_of_turn_delay" not in metrics:
        return
    logger.info("TIMING end-of-turn → transcript %.2fs after the caller stopped, turn "
                "ended %.2fs after", metrics.get("transcription_delay", 0.0),
                metrics["end_of_turn_delay"])


def session_kwargs(ud: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """The per-call extras for `AgentSession`, from what `prewarm` built.

    Split out of `entrypoint` so a test can see what the session is handed
    without a room: chiefly that the turn detector the worker built actually
    reaches it, since a detector that is constructed and then not passed is
    exactly the silent failure that left MAX_ENDPOINTING_DELAY dead for months —
    and, until 09-09, passed in a way the framework ignored.
    """
    kwargs: dict[str, Any] = {}
    if settings.stt_on_inference:
        # The freight vocabulary, applied wherever the recogniser takes a term
        # list. Only offered on the Inference path: the batch Whisper plugin has
        # no such capability and the framework would only log that it skipped it.
        # `forward_chat_context` OFF: for models that take it (u3-rt-pro), the
        # framework would push every agent reply to the recogniser as context the
        # moment it is spoken — a mid-call settings update the plugin itself warns
        # "may reconnect upstream", i.e. a deaf moment right where the caller's
        # answer lands. Recognition was measured without it; turn it on only with
        # a feed dump in hand to prove nothing is lost.
        kwargs["stt_context_options"] = {
            "keyterms": ud.get("keyterms") or _stt_keyterms(settings),
            "forward_chat_context": False}
    # The detector rides INSIDE turn_handling. Passed as its own argument next
    # to turn_handling= it is silently ignored (one deprecation warning, then the
    # framework builds its own default detector — the local mini model outside
    # dev mode), which is what the 09-08 wiring did until 09-09: the hosted
    # model the log announced never reached the session. An explicit None is the
    # framework's documented off switch; leaving the key out means "default
    # detector", so TURN_DETECTOR=0 must send None.
    handling = turn_handling(settings)
    handling["turn_detection"] = ud.get("turn_detector")
    kwargs["turn_handling"] = handling
    # The framework's echo warm-up feeds the recogniser SILENCE for the first
    # seconds the agent speaks — the whole greeting, on a phone call — so a
    # caller talking over "what can I do for you?" lost the head of their
    # sentence. Off by default here; a phone leg has no echo path for it to
    # guard. None is how the framework spells off. See AEC_WARMUP_SECONDS.
    kwargs["aec_warmup_duration"] = settings.aec_warmup_seconds or None
    return kwargs


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    ud = ctx.proc.userdata
    session = AgentSession(
        vad=ud["vad"],
        stt=ud["stt"],
        tts=ud["tts"],
        **session_kwargs(ud, _settings),     # turn_handling (with the detector) included
    )
    agent = CarrierAgent(ud["repo"], ud["composer"], ud["tts"],
                         fillers=ud.get("fillers"), greeting=ud.get("greeting"),
                         reask=ud.get("reask"), ctx=ctx,
                         hearing_trouble=ud.get("hearing_trouble"),
                         still_there=ud.get("still_there"),
                         idle_close=ud.get("idle_close"))
    _CALL_ID.set(agent.brain.call_id)
    session.on("metrics_collected", agent.on_metrics_collected)
    session.on("user_input_transcribed", agent.on_user_input_transcribed)
    session.on("user_state_changed", agent.on_user_state_changed)

    async def finalize_on_disconnect() -> None:
        # The transcript is only written at end_call, and most calls end with
        # the CALLER hanging up — without this, every such call stays an open
        # row and its transcript is lost to the audit trail. `abandon()` is a
        # no-op when the call already concluded properly.
        try:
            await asyncio.to_thread(agent.brain.abandon)
            logger.info("call %s finalized: %s (%d turns)", agent.brain.call_id,
                        agent.brain.outcome.value if agent.brain.outcome else "?",
                        len(agent.brain.transcript))
        except Exception:  # noqa: BLE001 - shutdown must never raise
            logger.exception("could not finalize call %s", agent.brain.call_id)
        if _settings.record_calls:
            # The recorder closes its file on its own thread; give it a moment
            # before concluding there is nothing to copy.
            saved = None
            for attempt in range(4):
                saved = await asyncio.to_thread(
                    save_call_recording, ctx.session_directory,
                    agent.brain.call_id, _settings.db_path, warn=(attempt == 3))
                if saved:
                    break
                await asyncio.sleep(0.5)
            if saved:
                logger.info("call %s recording saved: %s", agent.brain.call_id, saved)

    ctx.add_shutdown_callback(finalize_on_disconnect)
    nc = ud.get("noise_cancellation")
    room_input = RoomInputOptions(noise_cancellation=nc) if nc else RoomInputOptions()
    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=room_input,
        # Audio only, and EXPLICIT either way: not-given would defer to a
        # server-side flag, and traces/logs/transcript are observability
        # uploads this deployment hasn't opted into. See RECORD_CALLS in
        # settings.py for the consent and retention notes.
        record=({"audio": True, "traces": False, "logs": False,
                 "transcript": False} if _settings.record_calls else False),
    )


def main() -> None:
    for name, value in (("STT_PROVIDER", _settings.stt_provider),
                        ("TTS_PROVIDER", _settings.tts_provider)):
        if value.strip().lower() not in _SPEECH_PROVIDERS:
            raise RuntimeError(
                f"{name}={value!r} is not one of: {', '.join(_SPEECH_PROVIDERS)}.")
    if _settings.interruption_mode.strip().lower() not in _INTERRUPTION_MODES:
        raise RuntimeError(
            f"INTERRUPTION_MODE={_settings.interruption_mode!r} is not one of: "
            f"{', '.join(_INTERRUPTION_MODES)}.")
    # LiveKit is always required — it carries the call and, by default, the
    # speech. OpenRouter is required only while some AI hop still runs there.
    required = ["livekit_url", "livekit_api_key", "livekit_api_secret"]
    if _settings.needs_openrouter:
        required.append("openrouter_api_key")
    _settings.require(*required)
    if _settings.use_llm and not _settings.llm_api_key:
        raise RuntimeError(
            f"LLM_PROVIDER={_settings.llm_provider} needs "
            f"{_settings.llm_key_name}. Set it in .env, switch provider, or set "
            "USE_LLM=false to drive the flow with the offline stub."
        )
    capacity: dict[str, Any] = {}
    if _settings.max_concurrent_calls > 0:
        # The worker's "load" is calls in progress over the cap, and it is full
        # at the cap — see `call_load`. Passed as plain numbers so it applies in
        # dev mode too, where the framework's own threshold is infinite.
        capacity = {"load_fnc": _report_call_load,
                    "load_threshold": full_threshold(_settings.max_concurrent_calls)}
        logger.info("capacity: up to %d calls at once on this machine",
                    _settings.max_concurrent_calls)
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        **capacity,
        # Keep one process warm from boot. The dev-mode default is ZERO, which
        # made the first caller pay the whole cold start — Transport Pro auth,
        # the VAD model, TTS warmup — as 8-15 seconds of ringing into silence.
        num_idle_processes=1,
        # Prewarm composes the greeting (one LLM call) and renders it plus the
        # filler clips, so give it well over the 10s default before the
        # supervisor calls it hung.
        initialize_process_timeout=45.0,
    ))


if __name__ == "__main__":
    main()
