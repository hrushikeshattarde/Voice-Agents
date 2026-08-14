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
import threading

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
# Worker lifecycle
# --------------------------------------------------------------------------- #
def prewarm(proc):
    # Transport Pro or the offline seed data, per DATA_SOURCE. Built once per
    # worker process and shared by every call it handles — the repository caches
    # reads briefly and handles its own concurrency.
    proc.userdata["repo"] = build_repository(_settings)
    # VAD tuned to ignore background noise: require clearer, slightly longer
    # speech before it counts as a turn (defaults are 0.5 / 0.05s — too twitchy
    # for a noisy phone line).
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=0.6,
        min_speech_duration=0.2,
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
    def __init__(self, repo: Repository, composer):
        super().__init__(instructions="Carrier sales agent (logic in conversation layer).")
        self.brain = CarrierSalesAgent(repo, composer, _settings)

    async def on_enter(self):
        greeting = self.brain.greeting()
        logger.info("GREETING → %s", greeting)
        await self.session.say(greeting)

    async def on_user_turn_completed(self, turn_ctx, new_message):
        user_text = (getattr(new_message, "text_content", None) or "").strip()
        # Ignore empty fragments and Whisper silence-hallucinations ("Thank you.",
        # "you", "so"…) so the agent waits for real speech instead of replying to a phantom.
        if len(user_text) < 2 or parsing.is_probably_noise(user_text):
            logger.debug("Ignoring noise/empty transcript: %r", user_text)
            raise StopResponse()
        logger.info("CALLER said → %s", user_text)
        reply = await asyncio.to_thread(self.brain.handle, user_text)
        logger.info("AGENT reply → %s", reply)
        try:
            await self.session.say(reply)
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
    )
    nc = ud.get("noise_cancellation")
    room_input = RoomInputOptions(noise_cancellation=nc) if nc else RoomInputOptions()
    await session.start(
        agent=CarrierAgent(ud["repo"], ud["composer"]),
        room=ctx.room,
        room_input_options=room_input,
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
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == "__main__":
    main()
