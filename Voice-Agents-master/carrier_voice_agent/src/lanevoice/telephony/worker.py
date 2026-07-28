"""
LiveKit worker — connects the deterministic brain to real phone calls.

Pipeline:  phone -> LiveKit SIP -> this worker
           Silero VAD -> Groq Whisper STT -> CarrierSalesAgent -> Groq TTS

Run:  lanevoice-worker dev      (local)
      lanevoice-worker start    (production)
"""

from __future__ import annotations

import asyncio

import numpy as np
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.agents import tts as lk_tts
from livekit.plugins import groq as lk_groq
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
from lanevoice.db import Database, Repository
from lanevoice.logging_config import get_logger, setup_logging
from lanevoice.settings import get_settings
from lanevoice.voice import GroqComposer, GroqTTS, StubComposer

# Runtime setup (kept below imports so linting stays clean). load_dotenv() runs
# before get_settings() so a local .env populates the environment first.
load_dotenv()
_settings = get_settings()
setup_logging(_settings.log_level)
logger = get_logger("lanevoice.worker")


# --------------------------------------------------------------------------- #
# TTS adapter: wrap GroqTTS in LiveKit's TTS interface
# --------------------------------------------------------------------------- #
class GroqTTSPlugin(lk_tts.TTS):
    def __init__(self):
        self._model = GroqTTS(_settings)
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=self._model.sample_rate, num_channels=1,
        )

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return _TTSStream(self, text, self._model, conn_options=conn_options)


class _TTSStream(lk_tts.ChunkedStream):
    def __init__(self, tts, text, model, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        super().__init__(tts=tts, input_text=text, conn_options=conn_options)
        self._model = model

    async def _run(self, output_emitter):
        wav = await asyncio.to_thread(self._model.synthesize, self.input_text)
        pcm16 = (np.clip(wav, -1, 1) * 32767).astype(np.int16).tobytes()
        output_emitter.initialize(
            request_id="tts", sample_rate=self._model.sample_rate,
            num_channels=1, mime_type="audio/pcm",
        )
        output_emitter.push(pcm16)
        output_emitter.flush()


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #
def prewarm(proc):
    db = Database(_settings.db_path)
    db.init(seed=True)
    proc.userdata["repo"] = Repository(db)
    # VAD tuned to ignore background noise: require clearer, slightly longer
    # speech before it counts as a turn (defaults are 0.5 / 0.05s — too twitchy
    # for a noisy phone line).
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=0.6,
        min_speech_duration=0.2,
    )
    proc.userdata["stt"] = lk_groq.STT(
        model=_settings.stt_model,
        prompt=_settings.stt_prompt,   # bias Whisper toward freight vocabulary
    )
    proc.userdata["tts"] = GroqTTSPlugin()
    # The agent has no scripted lines, so the composer is what lets it talk at all.
    proc.userdata["composer"] = (
        GroqComposer(_settings) if _settings.use_llm else StubComposer(_settings))

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
    _settings.require(
        "livekit_url", "livekit_api_key", "livekit_api_secret", "groq_api_key"
    )
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == "__main__":
    main()
