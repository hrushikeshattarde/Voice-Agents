"""
LiveKit worker — connects the deterministic brain to real phone calls.

Pipeline:  phone -> LiveKit SIP -> this worker
           Silero VAD -> streaming STT -> CarrierSalesAgent -> streaming TTS

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
import random
import shutil
import threading
from collections.abc import AsyncIterable, Coroutine
from pathlib import Path
from typing import Any

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

from lanevoice import parsing
from lanevoice.conversation import CarrierSalesAgent, is_closing_turn
from lanevoice.conversation.agent import compose_greeting
from lanevoice.datasource import build_repository
from lanevoice.db import Repository
from lanevoice.env import load_env
from lanevoice.logging_config import get_logger, setup_logging
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

_SPEECH_PROVIDERS = ("inference", "openrouter")


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
    "MC", "MC number", "USDOT", "DOT number", "load number", "rate con",
    "rate confirmation", "dry van", "reefer", "flatbed", "step deck", "power only",
    "deadhead", "lumper", "detention", "layover", "TONU", "book it", "all in",
    "per mile", "pickup", "delivery", "appointment", "dispatch", "broker",
    "carrier", "Circle Logistics",
)


def _stt_keyterms(settings: Settings) -> list[str]:
    extra = [term.strip() for term in settings.stt_keyterms.split(",") if term.strip()]
    return list(dict.fromkeys([*STT_KEYTERMS, *extra]))


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
    """
    if model.startswith("deepgram/nova"):
        return {"numerals": True, "filler_words": False}
    if model.startswith("assemblyai/"):
        return {"format_turns": True}
    return {}


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
# Call recording
# --------------------------------------------------------------------------- #
def save_call_recording(session_dir: Path, call_id: str,
                        db_path: str | Path) -> Path | None:
    """Copy the session recorder's finished file out of the job's temp dir.

    livekit-agents records to `<session_dir>/audio.ogg` and DELETES that whole
    directory when the job cleans up — the copy is what makes the call
    replayable from the dashboard. Runs in a shutdown callback, which the
    framework guarantees is after the recorder finalized the file and before
    the temp dir is removed. Best-effort like everything else in shutdown: a
    failed copy costs the replay, never the audit trail.
    """
    source = Path(session_dir) / "audio.ogg"
    if not source.is_file():
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
    clips = {text: (pcm, rate) for text, pcm, rate in
             prerender_clips(_settings, texts, proc.userdata["tts"])} if texts else {}
    proc.userdata["greeting"] = (
        (greeting_text, *clips[greeting_text])
        if greeting_text and greeting_text in clips else None)
    proc.userdata["fillers"] = [
        (text, *clips[text]) for text in FILLER_LINES if text in clips]
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


class CarrierAgent(Agent):
    def __init__(self, repo: Repository, composer, tts: lk_tts.TTS,
                 fillers: list[Clip] | None = None,
                 greeting: Clip | None = None):
        super().__init__(instructions="Carrier sales agent (logic in conversation layer).")
        self.brain = CarrierSalesAgent(repo, composer, _settings)
        self._tts = tts
        self._fillers = list(fillers or [])
        self._greeting = greeting
        self._last_filler: int | None = None

    def _next_filler(self) -> Clip:
        """A filler that isn't the one just used — the same 'one sec' twice in a
        row is what makes a caller notice it's canned."""
        choices = [i for i in range(len(self._fillers)) if i != self._last_filler]
        self._last_filler = random.choice(choices or [0])
        return self._fillers[self._last_filler]

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

    async def _acknowledge_if_slow(self, reply_task: asyncio.Task) -> None:
        """Fill the composing gap with a spoken acknowledgment, never silence.

        Waits FILLER_DELAY for the reply; if it isn't ready, plays a cached clip
        while composition keeps running in its thread. The say() is awaited so a
        ready reply queues naturally behind it instead of colliding with it.
        """
        if not self._fillers or _settings.filler_delay <= 0:
            return
        done, _ = await asyncio.wait({reply_task}, timeout=_settings.filler_delay)
        if done:
            return
        text, pcm, rate = self._next_filler()
        try:
            await self.session.say(
                text,
                audio=_pcm_frames(pcm, rate),
                add_to_chat_ctx=False,   # phatic — not part of the record
            )
        except RuntimeError:
            pass                          # session closing; the reply say() will report

    async def on_enter(self):
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
        await speech
        if getattr(speech, "interrupted", False):
            logger.info("PLAYBACK CUT by caller → %s", greeting)
            await asyncio.to_thread(self.brain.note_playback_cut, greeting)

    async def on_user_turn_completed(self, turn_ctx, new_message):
        user_text = (getattr(new_message, "text_content", None) or "").strip()
        # Ignore empty fragments and transcriber hallucinations ("Thank you.",
        # "you", "so"…) so the agent waits for real speech instead of replying to a phantom.
        if len(user_text) < 2 or parsing.is_probably_noise(user_text):
            logger.debug("Ignoring noise/empty transcript: %r", user_text)
            raise StopResponse()
        logger.info("CALLER said → %s", user_text)
        reply_task = asyncio.create_task(asyncio.to_thread(self.brain.handle, user_text))
        # Every filler promises work is coming ("Alright, let me check that."),
        # and in front of a goodbye that promise is nonsense — observed live, a
        # caller's "No. Thank you." was answered with a filler and THEN the
        # close. A beat of silence before a goodbye is fine; skip the filler.
        if not is_closing_turn(user_text):
            await self._acknowledge_if_slow(reply_task)
        reply = await reply_task
        logger.info("AGENT reply → %s", reply)
        timing = self.brain.last_turn_timing
        if timing:
            logger.info(
                "TIMING brain → %.2fs (compose %.2fs over %d call%s; lookups and "
                "bookkeeping %.2fs) in %s",
                timing["total"], timing["compose"], timing["compose_calls"],
                "" if timing["compose_calls"] == 1 else "s", timing["other"],
                timing["state"])
        try:
            speech = self.session.say(reply)
            await speech
            # Barge-in cuts our audio mid-word. The transcript records what was
            # composed, so without this note the record shows a line the caller
            # may never have heard — observed live when a caller's "hello?"
            # (filling dead air) killed the very answer they were waiting on.
            if getattr(speech, "interrupted", False):
                logger.info("PLAYBACK CUT by caller → %s", reply)
                await asyncio.to_thread(self.brain.note_playback_cut, reply)
        except RuntimeError as e:  # e.g. caller hung up mid-turn
            logger.info("Could not speak (session closing): %s", e)
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


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    ud = ctx.proc.userdata
    session_kwargs: dict[str, Any] = {}
    if _settings.stt_on_inference:
        # The freight vocabulary, applied wherever the recogniser takes a term
        # list. Only offered on the Inference path: the batch Whisper plugin has
        # no such capability and the framework would only log that it skipped it.
        session_kwargs["stt_context_options"] = {"keyterms": _stt_keyterms(_settings)}
    session = AgentSession(
        vad=ud["vad"],
        stt=ud["stt"],
        tts=ud["tts"],
        turn_handling=turn_handling(_settings),
        **session_kwargs,
    )
    session.on("metrics_collected", _log_metrics)
    agent = CarrierAgent(ud["repo"], ud["composer"], ud["tts"],
                         fillers=ud.get("fillers"), greeting=ud.get("greeting"))

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
            saved = await asyncio.to_thread(
                save_call_recording, ctx.session_directory,
                agent.brain.call_id, _settings.db_path)
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
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
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
