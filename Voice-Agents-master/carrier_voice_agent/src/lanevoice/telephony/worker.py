"""
LiveKit worker — connects the deterministic brain to real phone calls.

Pipeline:  phone -> LiveKit SIP -> this worker
           Silero VAD -> Groq Whisper STT -> CarrierSalesAgent -> Groq TTS

The brain decides everything about the call except one thing it cannot know about:
whether there is a phone line to move. When it hands a call over it publishes a
`pending_transfer`, and this file does the moving — after the "putting you through"
line has finished playing, so the carrier hears it before the ringing starts.

Only a caller who ASKS for a person is put through. Every other reason the agent
can't finish a call is handed over as a callback — nobody's phone rings, and the
carrier is told a rep will ring them.

When it is a transfer, it is a warm one: the rep is dialled, briefed, and joined to
the call only once they accept (`telephony.whisper`). A rep who can't pick up gets
the agent back on the line telling the carrier they're busy and will call back.
With `WHISPER_ENABLED=0` it degrades to a blind transfer instead
(`telephony.transfer`), which connects the same two people with none of the context.

Run:  lanevoice-worker dev      (local)
      lanevoice-worker start    (production)
"""

from __future__ import annotations

import asyncio
import contextlib

import numpy as np
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
from lanevoice.datasource import build_repository
from lanevoice.db import Repository
from lanevoice.env import load_env
from lanevoice.logging_config import get_logger, setup_logging
from lanevoice.settings import get_settings
from lanevoice.telephony.transfer import TransferError, transfer_to_rep
from lanevoice.telephony.whisper import WhisperOutcome, whisper_and_bridge
from lanevoice.voice import GroqComposer, GroqTTS, StubComposer

# Runtime setup (kept below imports so linting stays clean). load_env() runs
# before get_settings() so a .env — found by searching upward from the working
# directory, not just in it — populates the environment first.
load_env()
_settings = get_settings()
setup_logging(_settings.log_level)
logger = get_logger("lanevoice.worker")


# --------------------------------------------------------------------------- #
# TTS adapter: wrap GroqTTS in LiveKit's TTS interface
# --------------------------------------------------------------------------- #
class GroqTTSPlugin(lk_tts.TTS):
    def __init__(self):
        self._model = GroqTTS(_settings)
        # Exposed because the whisper briefing is not spoken through the session —
        # it goes into a different room entirely, as raw PCM.
        self.model = self._model
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
    def __init__(self, repo: Repository, composer, room, tts_model=None):
        super().__init__(instructions="Carrier sales agent (logic in conversation layer).")
        self.brain = CarrierSalesAgent(repo, composer, _settings)
        self._room = room
        self._tts_model = tts_model
        # A call is handed over once. A second attempt after the first one finished
        # is a second set of phones ringing for a carrier who already has somebody.
        self._handed_over = False
        # Two humans are now talking to each other on this line. From here the agent
        # must not speak or answer another turn: the state machine is DONE, and its
        # reply for DONE is "this call has ended, goodbye" — said out loud over a
        # carrier and a rep mid-sentence.
        self._bridged = False

    async def on_enter(self):
        greeting = self.brain.greeting()
        logger.info("GREETING → %s", greeting)
        await self.session.say(greeting)
        # The greeting itself can hand the call over — that is what happens when the
        # composer is unreachable and the agent cannot speak for itself.
        await self._hand_over_if_pending()

    async def on_user_turn_completed(self, turn_ctx, new_message):
        if self._bridged:
            # The carrier and the rep are talking. Whatever was just transcribed was
            # one of them speaking to the other, not to us.
            raise StopResponse()
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
        await self._hand_over_if_pending()
        raise StopResponse()   # we answered this turn ourselves; skip the LLM node

    async def _say_quietly(self, text: str) -> None:
        """Speak, tolerating a session that is already closing."""
        try:
            await self.session.say(text)
        except RuntimeError as e:
            logger.info("Could not speak (session closing): %s", e)

    async def _hand_over_if_pending(self) -> None:
        """Get the caller to a person, if the brain handed the call over.

        Called after the reply has finished playing: the carrier has just been told
        who they're going to and to hold, and dialling before that sentence lands
        would cut them off mid-word.

        A failure here is not the caller's problem to work out. The brain composes
        the true thing to say instead — it couldn't get them across, and somebody
        will ring them back — and the audit trail records what actually happened
        rather than a transfer that never did.
        """
        resolution = self.brain.pending_transfer
        if resolution is None or self._handed_over:
            return
        rep = resolution.rep

        if not _settings.sip_transfer_enabled:
            logger.info(
                "SIP_TRANSFER_ENABLED is off: the handoff to %s (%s) is logged and "
                "the caller stays on the line.",
                rep.name or rep.rep_id, rep.spoken_phone)
            self.brain.pending_transfer = None
            return

        self._handed_over = True
        # Whisper when we actually can. Without an outbound trunk the rep cannot be
        # dialled at all, and failing the whole handoff over a missing config value
        # would tell a carrier who asked for a person that we couldn't reach anyone —
        # when a blind transfer would have connected them. Degrade, loudly.
        if _settings.whisper_enabled and self._tts_model is not None:
            if _settings.livekit_sip_outbound_trunk_id.strip():
                await self._whisper_handover()
                return
            logger.error(
                "WHISPER_ENABLED is on but LIVEKIT_SIP_OUTBOUND_TRUNK_ID is empty, so "
                "%s cannot be dialled and briefed. Falling back to a blind transfer: "
                "the carrier still gets through, the rep gets no context. See "
                "docs/LIVE_SETUP.md B5.", rep.name or rep.rep_id)
        await self._blind_handover(resolution)

    async def _blind_handover(self, resolution) -> None:
        """Hand the caller's own leg over with a REFER. No briefing, no accept step."""
        rep = resolution.rep
        logger.info("TRANSFER (blind) → %s (%s)%s",
                    rep.name or rep.rep_id, rep.spoken_phone,
                    "" if not resolution.is_fallback else " [fallback rep]")
        try:
            await transfer_to_rep(self._room, resolution, _settings)
        except TransferError as exc:
            logger.error("transfer failed for call %s: %s", self.brain.call_id, exc)
            await self._say_failed(str(exc))
            return
        self._bridged = True
        self.brain.transfer_connected()

    async def _whisper_handover(self) -> None:
        """Ring the rep, brief them, and join them on the accept keypress.

        One rep, one attempt. If they can't pick up, the agent comes back on the
        line and tells the carrier they're busy and will call back — it does not go
        looking for somebody else. The carrier asked for the rep on their load, and
        being handed round three strangers is not what they asked for.
        """
        resolution = self.brain.pending_transfer
        if resolution is None or resolution.rep is None:
            return
        rep = resolution.rep
        logger.info("WHISPER → %s (%s)%s", rep.name or rep.rep_id, rep.spoken_phone,
                    "" if not resolution.is_fallback else " [fallback rep]")

        script = self.brain.whisper_script()
        logger.info("WHISPER script → %s", script)
        holding = self._start_hold_reassurance()
        try:
            outcome = await whisper_and_bridge(
                room=self._room,
                resolution=resolution,
                script=script,
                tts=self._tts_model,
                settings=_settings,
            )
        finally:
            await _cancel(holding)

        if outcome is WhisperOutcome.CONNECTED:
            self._bridged = True
            self.brain.transfer_connected()
            return
        if outcome is WhisperOutcome.FAILED:
            await self._say_failed("the rep could not be reached at all")
            return

        # DECLINED: rang out, voicemail, or they heard the briefing and didn't take
        # it. All three mean the same thing to the carrier — come back and say so.
        reply = await asyncio.to_thread(
            self.brain.transfer_declined,
            "no answer or no keypress after the briefing")
        logger.info("AGENT reply → %s", reply)
        await self._say_quietly(reply)

    async def _say_failed(self, why: str) -> None:
        reply = await asyncio.to_thread(self.brain.transfer_failed, why)
        logger.info("AGENT reply → %s", reply)
        await self._say_quietly(reply)

    def _start_hold_reassurance(self):
        """Speak to the carrier if getting the rep on the phone runs long.

        They are holding in silence — the briefing happens in a room they are not
        in — so a slow rep sounds exactly like a dropped call. §9.5's "never a
        dead-air disconnect" covers the hold too.
        """
        delay = _settings.whisper_reassure_after
        if delay <= 0:
            return None

        async def _reassure() -> None:
            await asyncio.sleep(delay)
            line = await asyncio.to_thread(self.brain.still_holding)
            if line:
                logger.info("HOLD → %s", line)
                await self._say_quietly(line)

        return asyncio.create_task(_reassure())


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
        # The room is passed in because a handoff moves SIP legs around it, and
        # finding them means looking at who else is in it. The TTS model is passed
        # separately from the session's plugin: the whisper briefing is rendered to
        # raw PCM and played in a different room, not spoken through this session.
        agent=CarrierAgent(ud["repo"], ud["composer"], ctx.room,
                           tts_model=getattr(ud["tts"], "model", None)),
        room=ctx.room,
        room_input_options=room_input,
    )


async def _cancel(task) -> None:
    """Stop a background task and wait for it, tolerating that it already finished."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def main() -> None:
    _settings.require(
        "livekit_url", "livekit_api_key", "livekit_api_secret", "groq_api_key"
    )
    # Not required, because a deployment can legitimately run without it — the
    # handoff degrades to a blind transfer. But silently losing the briefing
    # because an id is missing is exactly the kind of thing nobody notices.
    if _settings.whisper_enabled and not _settings.livekit_sip_outbound_trunk_id:
        logger.warning(
            "WHISPER_ENABLED is on but LIVEKIT_SIP_OUTBOUND_TRUNK_ID is not set. "
            "Reps cannot be dialled, so every handoff will fail rather than fall "
            "back. Create an outbound trunk (`lk sip outbound create "
            "sip_setup/outbound-trunk.json`) and set the id, or set "
            "WHISPER_ENABLED=0 for a blind transfer.")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == "__main__":
    main()
