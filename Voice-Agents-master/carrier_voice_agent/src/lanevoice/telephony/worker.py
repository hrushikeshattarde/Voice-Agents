"""
LiveKit worker — connects the deterministic brain to real phone calls.

Pipeline:  phone -> LiveKit SIP -> this worker
           Silero VAD -> OpenRouter Whisper STT -> CarrierSalesAgent
                      -> OpenRouter TTS

Every AI hop runs on OpenRouter: transcription, the composer that writes each
spoken turn, and the voice that says it. One key, three endpoints.

Run:  lanevoice-worker dev      (local)
      lanevoice-worker start    (production)
"""

from __future__ import annotations

import asyncio
import random
import shutil
import threading
from pathlib import Path

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.agents import tts as lk_tts
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
from lanevoice.conversation import CarrierSalesAgent
from lanevoice.datasource import build_repository
from lanevoice.db import Repository
from lanevoice.env import load_env
from lanevoice.logging_config import get_logger, setup_logging
from lanevoice.settings import get_settings
from lanevoice.voice import OpenRouterTTS, build_composer

# Runtime setup (kept below imports so linting stays clean). load_env() runs
# before get_settings() so a .env — found by searching upward from the working
# directory, not just in it — populates the environment first.
load_env()
_settings = get_settings()
setup_logging(_settings.log_level)
logger = get_logger("lanevoice.worker")


# --------------------------------------------------------------------------- #
# TTS adapter: wrap OpenRouterTTS in LiveKit's TTS interface
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
    per-second one. `stream_pcm` has the measurements, and the reason that floor
    is why splitting the text into sentences would make this worse, not better.

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
# Dead-air fillers
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


def _synthesize_fillers(tts: OpenRouterTTSPlugin) -> list[tuple[str, bytes]]:
    """Every filler clip as (text, raw PCM), or fewer if some fail to render.

    A clip that won't synthesize costs the feature, never the worker: the agent
    without fillers is the agent we had yesterday.
    """
    clips: list[tuple[str, bytes]] = []
    for text in FILLER_LINES:
        try:
            clips.append((text, b"".join(tts._model.stream_pcm(text))))
        except Exception as exc:  # noqa: BLE001 - degrade, don't die
            logger.warning("filler clip %r failed to synthesize (%s)", text, exc)
    return clips


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
    # turn it and why.
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=_settings.vad_activation_threshold,
        min_speech_duration=_settings.vad_min_speech_duration,
    )
    # OpenRouter's `/audio/transcriptions` is OpenAI-shaped, so the OpenAI plugin
    # drives it verbatim once it is pointed at the gateway — no bespoke STT class.
    # The model is a namespaced OpenRouter slug (`openai/whisper-large-v3`), which
    # is also why the plugin sends `response_format=json` rather than the
    # `verbose_json` it reserves for a model named exactly `whisper-1`; OpenRouter
    # accepts both.
    #
    # `prompt` is documented by OpenRouter as accepted and IGNORED, so the freight
    # vocabulary in STT_PROMPT is not biasing anything today. It costs nothing to
    # keep sending and starts working if that changes.
    proc.userdata["stt"] = lk_openai.STT(
        model=_settings.stt_model,
        base_url=_settings.openrouter_base_url,
        api_key=_settings.openrouter_api_key,
        language="en",
        prompt=_settings.stt_prompt,
    )
    proc.userdata["tts"] = OpenRouterTTSPlugin()
    # Filler clips ride the same voice, so the acknowledgment and the reply
    # sound like one person. Rendered here, at process start, so playing one
    # mid-call costs nothing.
    proc.userdata["fillers"] = (
        _synthesize_fillers(proc.userdata["tts"])
        if _settings.filler_delay > 0 else [])
    if _settings.filler_delay > 0:
        logger.info("dead-air fillers ready: %d clips (spoken when a reply "
                    "takes > %.1fs)", len(proc.userdata["fillers"]),
                    _settings.filler_delay)
    # The agent has no scripted lines, so the composer is what lets it talk at all.
    # `build_composer` picks the provider from LLM_PROVIDER and falls back to the
    # offline stub when USE_LLM is off or the provider's key is missing.
    proc.userdata["composer"] = build_composer(_settings)
    logger.info("composer: %s / %s", _settings.llm_provider,
                _settings.resolved_llm_model)

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
    def __init__(self, repo: Repository, composer,
                 fillers: list[tuple[str, bytes]] | None = None):
        super().__init__(instructions="Carrier sales agent (logic in conversation layer).")
        self.brain = CarrierSalesAgent(repo, composer, _settings)
        self._fillers = list(fillers or [])
        self._last_filler: int | None = None

    def _next_filler(self) -> tuple[str, bytes]:
        """A filler that isn't the one just used — the same 'one sec' twice in a
        row is what makes a caller notice it's canned."""
        choices = [i for i in range(len(self._fillers)) if i != self._last_filler]
        self._last_filler = random.choice(choices or [0])
        return self._fillers[self._last_filler]

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
        text, pcm = self._next_filler()
        try:
            await self.session.say(
                text,
                audio=_pcm_frames(pcm, _settings.tts_sample_rate),
                add_to_chat_ctx=False,   # phatic — not part of the record
            )
        except RuntimeError:
            pass                          # session closing; the reply say() will report

    async def on_enter(self):
        greeting = self.brain.greeting()
        logger.info("GREETING → %s", greeting)
        speech = self.session.say(greeting)
        await speech
        if getattr(speech, "interrupted", False):
            logger.info("PLAYBACK CUT by caller → %s", greeting)
            await asyncio.to_thread(self.brain.note_playback_cut, greeting)

    async def on_user_turn_completed(self, turn_ctx, new_message):
        user_text = (getattr(new_message, "text_content", None) or "").strip()
        # Ignore empty fragments and Whisper silence-hallucinations ("Thank you.",
        # "you", "so"…) so the agent waits for real speech instead of replying to a phantom.
        if len(user_text) < 2 or parsing.is_probably_noise(user_text):
            logger.debug("Ignoring noise/empty transcript: %r", user_text)
            raise StopResponse()
        logger.info("CALLER said → %s", user_text)
        reply_task = asyncio.create_task(asyncio.to_thread(self.brain.handle, user_text))
        await self._acknowledge_if_slow(reply_task)
        reply = await reply_task
        logger.info("AGENT reply → %s", reply)
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


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    ud = ctx.proc.userdata
    session = AgentSession(
        vad=ud["vad"],
        stt=ud["stt"],
        tts=ud["tts"],
        allow_interruptions=_settings.allow_interruptions,
        min_endpointing_delay=_settings.min_endpointing_delay,
        max_endpointing_delay=_settings.max_endpointing_delay,
        # Short line-checks ("hello?") must not cut the agent's audio; a caller
        # genuinely talking over it still should. See the settings comments.
        min_interruption_duration=_settings.min_interruption_duration,
        resume_false_interruption=_settings.resume_false_interruption,
        false_interruption_timeout=_settings.false_interruption_timeout,
    )
    agent = CarrierAgent(ud["repo"], ud["composer"], fillers=ud.get("fillers"))

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
    # OpenRouter is always required: STT and TTS run on it whichever LLM composes.
    _settings.require(
        "livekit_url", "livekit_api_key", "livekit_api_secret", "openrouter_api_key"
    )
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
        # Prewarm now also renders the filler clips (a few TTS calls), so give
        # it more than the 10s default before the supervisor calls it hung.
        initialize_process_timeout=30.0,
    ))


if __name__ == "__main__":
    main()
