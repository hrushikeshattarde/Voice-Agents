"""
The warm transfer: brief the rep, then join them to the call (PRD §3 step 6b).

A blind transfer hands the carrier's line to a number and hopes. This does what a
person on a desk does — gets the rep on the phone first, tells them who is waiting
and why, and only connects them once they've said yes.

```
   room "call-abc"                    room "call-abc-whisper-2423"
   ┌───────────────┐                  ┌────────────────────────────┐
   │ carrier (SIP) │                  │ rep (SIP, dialled OUT)     │
   │ agent         │                  │ this module (audio only)   │
   └───────────────┘                  └────────────────────────────┘
           ▲                                        │
           └──── on the accept digit: ──────────────┘
                 move_participant(rep → main room)
```

**Two rooms, on purpose.** The briefing names our last offer and sometimes says
the carrier was flagged for fraud review — the carrier must not hear a word of it.
Muting is a setting somebody can get wrong; a room the carrier is not in is a
structural guarantee. The rep is only ever moved into the carrier's room once they
have accepted, and at that moment the briefing has finished playing.

Three consequences worth knowing before this goes live:

* **It dials out.** The rep is called, not REFERred to, so this needs an OUTBOUND
  trunk (`LIVEKIT_SIP_OUTBOUND_TRUNK_ID`) as well as the inbound one that answers
  carriers. See `sip_setup/outbound-trunk.json`.
* **An extension is not part of a phone number.** A rep recorded as
  `312-300-7447 ext8754` needs the 8754 delivered separately — as keypresses after
  the call is answered, or as the SIP user on a PBX trunk. `dial_plan` picks which.
* **The carrier is on hold, in silence,** for as long as the rep's phone rings plus
  the briefing. The worker speaks to them if it runs long — §9.5's "never a
  dead-air disconnect" applies to a hold as much as to a hang-up.
* **Voicemail is a decline.** An answering machine picks up, hears the whisper, and
  presses nothing; the decision timeout expires and the agent goes back to the
  carrier to say the rep is busy — rather than introducing them to a beep.

`WhisperGate` is separated out and pure, because "what does this keypress mean" is
the part with rules in it and it should not need a phone line to test.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import Enum
from typing import Any

import numpy as np

from lanevoice.domain.models import TransferResolution
from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings
from lanevoice.telephony.transfer import TransferError, dial_plan

logger = get_logger(__name__)

# Audio is pushed to LiveKit in 10ms frames, which is what its SIP path expects.
_FRAME_MS = 10


class WhisperOutcome(str, Enum):
    CONNECTED = "connected"   # the rep accepted; the two legs are together
    DECLINED = "declined"     # no answer, no keypress, voicemail, or they hung up
    FAILED = "failed"         # we could not even make the attempt


class WhisperAction(str, Enum):
    ACCEPT = "accept"
    REPEAT = "repeat"
    IGNORE = "ignore"


class WhisperGate:
    """What a keypress from the rep means.

    Deliberately dumb and deliberately pure. Anything that isn't the accept or
    repeat digit is IGNORED rather than treated as a refusal: a rep fumbling for 9
    and hitting 8 has not declined the call, and the decision timeout is what
    handles somebody who really isn't there.

    Repeats are capped. A rep pressing repeat forever would hold a carrier on a
    silent line indefinitely, so past the cap it stops repeating and the timeout
    takes over.
    """

    def __init__(self, *, accept: str = "9", repeat: str = "1", max_repeats: int = 2):
        self.accept = str(accept).strip()
        self.repeat = str(repeat).strip()
        self.max_repeats = max(0, int(max_repeats))
        self.repeats_used = 0

    @property
    def repeats_left(self) -> int:
        return max(0, self.max_repeats - self.repeats_used)

    def on_digit(self, digit: str | None) -> WhisperAction:
        pressed = str(digit or "").strip()
        if not pressed:
            return WhisperAction.IGNORE
        if pressed == self.accept:
            return WhisperAction.ACCEPT
        if pressed == self.repeat:
            if self.repeats_left == 0:
                logger.info("rep asked for the briefing again past the cap of %d; "
                            "not repeating.", self.max_repeats)
                return WhisperAction.IGNORE
            self.repeats_used += 1
            return WhisperAction.REPEAT
        logger.info("rep pressed %r, which is neither accept (%s) nor repeat (%s) — "
                    "ignoring it.", pressed, self.accept, self.repeat)
        return WhisperAction.IGNORE


def whisper_room_name(main_room: str, rep_id: str) -> str:
    """The booth this rep is briefed in. Per rep, so an escalation gets a clean one."""
    return f"{main_room}-whisper-{rep_id}"


def _frames(pcm: bytes, sample_rate: int) -> list[bytes]:
    """Split PCM16 mono into the fixed-size frames LiveKit wants."""
    per_frame = int(sample_rate / 1000 * _FRAME_MS) * 2      # 2 bytes per sample
    if per_frame <= 0:
        return [pcm]
    chunks = [pcm[i:i + per_frame] for i in range(0, len(pcm), per_frame)]
    # The tail is padded rather than dropped: a short final frame is rejected, and
    # dropping it clips the last syllable — which here is the digit to press.
    if chunks and len(chunks[-1]) < per_frame:
        chunks[-1] = chunks[-1] + b"\x00" * (per_frame - len(chunks[-1]))
    return chunks


def render(tts: Any, text: str) -> tuple[bytes, int]:
    """Synthesize the briefing once, as PCM16. Repeats replay these same bytes.

    Rendering per repeat would cost a round trip and could come back with slightly
    different wording emphasis; a rep asking to hear it *again* should hear the
    same thing again.
    """
    wav = tts.synthesize(text)
    pcm = (np.clip(np.asarray(wav, dtype=np.float32), -1, 1) * 32767
           ).astype(np.int16).tobytes()
    return pcm, int(getattr(tts, "sample_rate", 24000))


async def _play(source: Any, pcm: bytes, sample_rate: int) -> None:
    from livekit import rtc

    per_frame_samples = int(sample_rate / 1000 * _FRAME_MS)
    for chunk in _frames(pcm, sample_rate):
        await source.capture_frame(rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=per_frame_samples,
        ))


async def _wait_for_digit(
    digits: asyncio.Queue, hung_up: asyncio.Event, gate: WhisperGate, timeout: float
) -> WhisperAction | None:
    """The rep's decision, or None if they ran out of time or hung up.

    Keeps waiting through keypresses that mean nothing, but always inside the one
    overall deadline — otherwise a rep leaning on the keypad resets the clock and
    the carrier holds forever.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, timeout)
    hangup = asyncio.ensure_future(hung_up.wait())
    try:
        while (remaining := deadline - loop.time()) > 0:
            pressed = asyncio.ensure_future(digits.get())
            done, _ = await asyncio.wait(
                {pressed, hangup}, timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED)
            if pressed not in done:
                pressed.cancel()
                return None                      # hung up, or the deadline passed
            action = gate.on_digit(pressed.result())
            if action is not WhisperAction.IGNORE:
                return action
        return None
    finally:
        hangup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hangup


async def whisper_and_bridge(
    *,
    room: Any,
    resolution: TransferResolution,
    script: str,
    tts: Any,
    settings: Settings,
) -> WhisperOutcome:
    """Brief `resolution.rep`, and join them to the call if they accept.

    Returns CONNECTED, DECLINED (try the next rep) or FAILED (nothing to try).
    Never raises for an ordinary telephony failure — the caller has a carrier on
    hold and needs an answer, not an exception.
    """
    rep = resolution.rep
    # Where to dial, and the extension to send once somebody answers. Which of the
    # two carries the extension depends on what picks up — see `dial_plan` and
    # WHISPER_EXTENSION_MODE.
    call_to, extension_digits = dial_plan(
        rep, settings.whisper_extension_mode) if rep else ("", "")
    if not call_to:
        logger.error("cannot brief a rep with nothing to dial (%s)",
                     rep.name if rep else "nobody resolved")
        return WhisperOutcome.FAILED
    trunk = settings.livekit_sip_outbound_trunk_id.strip()
    if not trunk:
        logger.error(
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID is not set, so the rep cannot be dialled "
            "and the call cannot be whispered. Create an outbound trunk "
            "(`lk sip outbound create sip_setup/outbound-trunk.json`) and set the "
            "id, or set WHISPER_ENABLED=0 to fall back to a blind transfer.")
        return WhisperOutcome.FAILED

    from livekit import api, rtc

    booth_name = whisper_room_name(room.name, rep.rep_id)
    rep_identity = f"rep-{rep.rep_id}"
    # Rendered before anything is dialled: it costs a TTS round trip, and doing it
    # while the rep's phone is already ringing is silence they'd hear.
    pcm, sample_rate = await asyncio.to_thread(render, tts, script)

    lkapi = api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    booth = rtc.Room()
    digits: asyncio.Queue[str] = asyncio.Queue()
    hung_up = asyncio.Event()
    connected = False

    try:
        token = (
            api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
            .with_identity("whisper")
            .with_name("Whisper")
            .with_grants(api.VideoGrants(
                room_join=True, room=booth_name,
                can_publish=True, can_subscribe=True))
            .to_jwt()
        )

        @booth.on("sip_dtmf_received")
        def _on_dtmf(event: Any) -> None:
            digits.put_nowait(getattr(event, "digit", "") or "")

        @booth.on("participant_disconnected")
        def _on_left(participant: Any) -> None:
            if getattr(participant, "identity", "") == rep_identity:
                hung_up.set()

        await booth.connect(settings.livekit_url, token)
        source = rtc.AudioSource(sample_rate, 1)
        await booth.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("whisper", source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

        logger.info("dialling %s%s for %s", call_to,
                    f" then sending extension {extension_digits}"
                    if extension_digits else "", rep.name or rep.rep_id)
        try:
            await lkapi.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk,
                    sip_call_to=call_to,
                    room_name=booth_name,
                    participant_identity=rep_identity,
                    participant_name=rep.name or f"rep {rep.rep_id}",
                    play_ringtone=True,
                    # Keypresses sent once the call is answered, for an
                    # auto-attendant that wants the extension. Empty for a direct
                    # number or a PBX that already routed on it.
                    dtmf=extension_digits,
                    # Block until they actually pick up, so a briefing is never
                    # played into a ringing phone.
                    wait_until_answered=True,
                ),
                timeout=settings.whisper_ring_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - every dial failure escalates alike
            # Busy, declined, no answer, or a trunk that won't place the call. All
            # of them mean this rep does not have the carrier, which is the next
            # rep's problem — the reason is logged for whoever reads it later.
            logger.warning("could not get %s on the line: %s: %s",
                           rep.name or rep.rep_id, type(exc).__name__, exc)
            return WhisperOutcome.DECLINED

        gate = WhisperGate(
            accept=settings.whisper_accept_digit,
            repeat=settings.whisper_repeat_digit,
            max_repeats=settings.whisper_max_repeats,
        )
        while True:
            await _play(source, pcm, sample_rate)
            action = await _wait_for_digit(
                digits, hung_up, gate, settings.whisper_decision_seconds)
            if action is WhisperAction.ACCEPT:
                break
            if action is WhisperAction.REPEAT:
                logger.info("%s asked to hear the briefing again (%d left)",
                            rep.name or rep.rep_id, gate.repeats_left)
                continue
            logger.info(
                "%s did not take the call (%s) — escalating.",
                rep.name or rep.rep_id,
                "hung up" if hung_up.is_set() else "no keypress in "
                f"{settings.whisper_decision_seconds:g}s")
            return WhisperOutcome.DECLINED

        await lkapi.room.move_participant(api.MoveParticipantRequest(
            room=booth_name, identity=rep_identity, destination_room=room.name))
        connected = True
        logger.info("%s took the call — bridged into %s",
                    rep.name or rep.rep_id, room.name)
        return WhisperOutcome.CONNECTED

    except Exception as exc:  # noqa: BLE001 - a broken booth must not raise on-call
        logger.error("whisper handoff to %s failed: %s: %s",
                     rep.name or rep.rep_id, type(exc).__name__, exc)
        return WhisperOutcome.FAILED
    finally:
        with contextlib.suppress(Exception):
            await booth.disconnect()
        if not connected:
            # Deleting the booth is also what stops a leg we gave up on: when the
            # ring timeout fires, LiveKit's outbound call is still ringing the rep's
            # phone, and without this they answer to nobody after we've moved on.
            #
            # Only ever on the failure paths, though. Tearing the room down after a
            # successful bridge risks taking the rep's leg with it, and an empty
            # room is reaped by LiveKit on its own anyway.
            with contextlib.suppress(Exception):
                await lkapi.room.delete_room(api.DeleteRoomRequest(room=booth_name))
        with contextlib.suppress(Exception):
            await lkapi.aclose()


__all__ = [
    "TransferError",
    "WhisperAction",
    "WhisperGate",
    "WhisperOutcome",
    "render",
    "whisper_and_bridge",
    "whisper_room_name",
]
