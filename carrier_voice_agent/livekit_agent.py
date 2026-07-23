"""
livekit_agent.py
----------------
DEPLOYMENT SCAFFOLD — connects the same brain (conversation.py) to real phone
calls via LiveKit Agents + Twilio SIP.

This is NOT meant to run inside Colab for live inbound calls (Colab has no
stable public endpoint and sessions time out). Run it on a small GPU host
(the LiveKit worker connects OUT to LiveKit Cloud, so it needs no inbound
ports of its own).

Pipeline:  Twilio PSTN  ->  Twilio SIP trunk  ->  LiveKit SIP  ->  this worker
           worker:  Silero VAD -> Whisper STT -> (our state machine) -> Kokoro TTS

Install (on the GPU host, Python 3.11+):
    pip install "livekit-agents>=1.0" livekit-plugins-silero \
                faster-whisper kokoro soundfile transformers torch

Env vars required (see README "API keys"):
    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET

Run:
    python livekit_agent.py dev        # local dev
    python livekit_agent.py start      # production worker
"""

import asyncio
import logging
import os
import sys
import numpy as np

# Load secrets from a local .env file if present (pip install python-dotenv).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Quiet the extremely chatty Windows SAPI/COM bindings (pyttsx3) so real logs
# are readable. Runs at import so it also applies in prewarmed subprocesses.
for _noisy in ("comtypes", "comtypes.client", "comtypes._post_coinit",
               "comtypes.tools", "comtypes._vtbl", "comtypes.client._generate"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("carrier-agent")


def _check_env():
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
               if not os.getenv(k)]
    if missing:
        sys.exit(
            "\nMissing required env vars: " + ", ".join(missing) +
            "\nCopy .env.example to .env and fill in your LiveKit values "
            "(or set them in your shell).\n")


from livekit import rtc
from livekit.agents import (
    Agent, AgentSession, JobContext, WorkerOptions, cli,
    stt as lk_stt, tts as lk_tts,
)
from livekit.plugins import silero

# APIConnectOptions default — required by ChunkedStream/STT in livekit-agents 1.6.
try:
    from livekit.agents import DEFAULT_API_CONNECT_OPTIONS
except ImportError:
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from voice_pipeline import WhisperSTT, build_tts, HFPhraser
from conversation import CarrierSalesAgent


# --------------------------------------------------------------------------- #
# Thin adapters wrapping our HF models in LiveKit's STT/TTS interfaces.
# (LiveKit's plugin API evolves; verify method names against your installed
#  livekit-agents version. These follow the v1.x streaming conventions.)
# --------------------------------------------------------------------------- #
class LocalWhisperSTT(lk_stt.STT):
    def __init__(self):
        super().__init__(capabilities=lk_stt.STTCapabilities(
            streaming=False, interim_results=False))
        self._model = WhisperSTT(os.getenv("WHISPER_SIZE", "base.en"))

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        frame = rtc.combine_audio_frames(buffer)
        samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
        # resample to 16k if needed (Whisper wants 16k)
        text = await asyncio.to_thread(self._model.transcribe, samples, 16000)
        return lk_stt.SpeechEvent(
            type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[lk_stt.SpeechData(text=text, language="en")],
        )


class LocalKokoroTTS(lk_tts.TTS):
    def __init__(self):
        self._model = build_tts()   # pyttsx3 on Windows, Kokoro on Linux/Colab
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False),
            sample_rate=self._model.sample_rate, num_channels=1)

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return _KokoroStream(self, text, self._model, conn_options=conn_options)


class _KokoroStream(lk_tts.ChunkedStream):
    def __init__(self, tts, text, model, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        super().__init__(tts=tts, input_text=text, conn_options=conn_options)
        self._model = model

    async def _run(self, output_emitter):
        wav = await asyncio.to_thread(self._model.synthesize, self.input_text)
        pcm16 = (np.clip(wav, -1, 1) * 32767).astype(np.int16).tobytes()
        output_emitter.initialize(
            request_id="tts", sample_rate=self._model.sample_rate,
            num_channels=1, mime_type="audio/pcm")
        output_emitter.push(pcm16)
        output_emitter.flush()


# StopResponse tells the session "I answered this turn myself; don't run an LLM".
try:
    from livekit.agents import StopResponse
except ImportError:  # older/newer layout
    from livekit.agents.llm import StopResponse

# Turn the phrasing LLM on only if you explicitly ask for it (it's a ~3GB
# download and slow on CPU). Default OFF -> clean template replies, fast calls.
USE_LLM = os.getenv("AGENT_USE_LLM") == "1"


# --------------------------------------------------------------------------- #
# Prewarm: load the heavy models ONCE per worker process, before any call —
# so the caller isn't sitting in silence while weights download/load.
# --------------------------------------------------------------------------- #
def prewarm(proc):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["stt"] = LocalWhisperSTT()
    proc.userdata["tts"] = LocalKokoroTTS()
    proc.userdata["phraser"] = HFPhraser() if USE_LLM else None


# --------------------------------------------------------------------------- #
# The agent: bridges LiveKit's turn events into our deterministic brain.
# --------------------------------------------------------------------------- #
class CarrierAgent(Agent):
    def __init__(self, phraser=None):
        super().__init__(instructions="Carrier sales agent (logic in conversation.py).")
        self.brain = CarrierSalesAgent(llm=phraser)

    async def on_enter(self):
        greeting = self.brain.greeting()
        logger.info("GREETING → %s", greeting)
        await self.session.say(greeting)

    async def on_user_turn_completed(self, turn_ctx, new_message):
        user_text = getattr(new_message, "text_content", None) or ""
        logger.info("CALLER said → %s", user_text)
        reply = await asyncio.to_thread(self.brain.handle, user_text)
        logger.info("AGENT reply → %s", reply)
        await self.session.say(reply)
        # We produced the reply ourselves — don't let the pipeline call an LLM.
        raise StopResponse()


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    ud = ctx.proc.userdata
    # Turn-taking buffer: how long to wait after the caller stops talking before
    # the agent responds. Larger = the caller can pause mid-sentence without being
    # cut off (also gives our CPU Whisper time to finish). Tune via .env.
    min_delay = float(os.getenv("MIN_ENDPOINTING_DELAY", "2.0"))
    max_delay = float(os.getenv("MAX_ENDPOINTING_DELAY", "10.0"))
    session = AgentSession(
        vad=ud["vad"],
        stt=ud["stt"],
        tts=ud["tts"],
        allow_interruptions=True,        # caller can talk over the agent
        min_endpointing_delay=min_delay,  # wait this long after a pause before replying
        max_endpointing_delay=max_delay,
        # No chat LLM node: our deterministic state machine drives every turn.
    )
    await session.start(agent=CarrierAgent(ud.get("phraser")), room=ctx.room)


if __name__ == "__main__":
    _check_env()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


# --------------------------------------------------------------------------- #
# TWILIO  ->  LIVEKIT SIP  wiring (one-time setup, done via CLI/console)
# --------------------------------------------------------------------------- #
"""
1. Buy a Twilio phone number (Voice-capable).

2. Create a Twilio Elastic SIP Trunk:
     - Termination URI: <your-name>.pstn.twilio.com
     - Origination:  point to LiveKit SIP URI  (sip:<project>.sip.livekit.cloud)
     - Assign your Twilio number to the trunk.

3. In LiveKit, create an inbound SIP trunk + dispatch rule so incoming calls
   are routed to a room your worker joins. Using the LiveKit CLI:

     lk sip inbound create --file inbound-trunk.json
     lk sip dispatch create --file dispatch-rule.json

   inbound-trunk.json:
     { "trunk": { "name": "twilio-in", "numbers": ["+1XXXXXXXXXX"] } }

   dispatch-rule.json:
     { "dispatch_rule": {
         "rule": { "dispatchRuleIndividual": { "roomPrefix": "call-" } } } }

4. Deploy this worker on a GPU host and run:  python livekit_agent.py start
   The worker dials OUT to LIVEKIT_URL, so no inbound firewall ports needed.

5. Call your Twilio number -> Twilio -> LiveKit -> this worker answers.
"""
