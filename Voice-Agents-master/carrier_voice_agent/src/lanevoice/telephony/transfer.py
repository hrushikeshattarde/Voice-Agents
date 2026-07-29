"""
Moving a live caller onto a rep's phone (PRD §3 step 6b / §9.5).

The conversation layer decides WHO takes a call — it is independent of audio I/O
and knows nothing about SIP, which is why `CarrierSalesAgent` publishes a
`pending_transfer` instead of dialling anything. This module is the other half:
given that decision, hand the caller's leg across.

The mechanism is a SIP REFER, issued through LiveKit:

    POST /twirp/livekit.SIP/TransferSIPParticipant
        room_name, participant_identity, transfer_to, play_dialtone

`participant_identity` is the CALLER, not the rep — we are moving the leg that
came in over SIP, so the identity is found by looking for the room's SIP
participant rather than by guessing at a naming convention.

`play_dialtone` is on. The agent has just said "hold a moment", and the seconds
while the rep's phone rings are exactly the dead air §9.5 says never to leave a
carrier sitting in.

Everything here raises `TransferError` and nothing here is fatal: a transfer that
cannot happen is a call the carrier is still on, and the agent has a true thing to
say about it (`CarrierSalesAgent.transfer_failed`).
"""

from __future__ import annotations

from typing import Any

from lanevoice.domain.models import Rep, TransferResolution
from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings

logger = get_logger(__name__)

# livekit.rtc.ParticipantKind.PARTICIPANT_KIND_SIP. Read off the enum at call
# time rather than pinned here — this is only the name of what we are looking for.
_SIP_KIND_NAME = "PARTICIPANT_KIND_SIP"


class TransferError(RuntimeError):
    """The caller's line could not be moved onto the rep's phone."""


def dial_target(rep: Rep | None) -> str | None:
    """A rep's phone as a SIP transfer destination, or None if we can't dial them.

    `tel:` is the scheme for a number reached over the configured trunk, which is
    what a rep's desk or mobile number is.

    An extension cannot travel in the destination, so a rep whose only number is a
    switchboard line plus an extension is transferred to the switchboard. That is
    genuinely the best the record supports; the extension is in the call note, and
    `Rep.extension` exists so it is never silently thrown away.
    """
    if rep is None or not rep.phone:
        return None
    return f"tel:{rep.phone}"


def dial_plan(rep: Rep | None, mode: str = "dtmf") -> tuple[str, str]:
    """How to reach a rep who sits behind an extension: `(sip_call_to, dtmf)`.

    Transport Pro records an office number as `312-300-7447 ext8754`. Dialling only
    the base number reaches a switchboard, so the extension has to be delivered
    somehow — and *how* depends entirely on what answers the call. There is no
    setting that is right for every desk, hence `WHISPER_EXTENSION_MODE`:

        dtmf      dial the full number, then send the extension as keypresses once
                  the call is answered. This is the route when an auto-attendant
                  picks up ("press or dial your party's extension"). LiveKit sends
                  the digits for us — the `dtmf` field on CreateSIPParticipant.

        sip_user  dial the EXTENSION as the SIP user, so the request becomes
                  `sip:8754@<your trunk address>`. This is the route when the trunk
                  points at your own phone system rather than at a carrier: the PBX
                  knows what 8754 means and rings that desk directly. Cleanest by
                  far, and no IVR timing to get wrong.

        off       base number only. The transfer reaches the switchboard and the
                  extension is left for the rep in the call note.

    Returns the two values to hand to `create_sip_participant`. A rep with no
    extension dials the same either way.
    """
    if rep is None or not (rep.phone or rep.extension):
        return "", ""
    choice = (mode or "dtmf").strip().lower()
    if not rep.extension or choice == "off":
        return rep.phone, ""
    if choice == "sip_user":
        # The extension IS the address on a PBX trunk. Falls back to the full
        # number when there is no extension, which a PBX routes outbound anyway.
        return rep.extension or rep.phone, ""
    return rep.phone, rep.extension


def caller_identity(room: Any) -> str | None:
    """The identity of the leg that dialled in — the one being transferred."""
    from livekit import rtc

    sip_kind = rtc.ParticipantKind.Value(_SIP_KIND_NAME)
    for participant in getattr(room, "remote_participants", {}).values():
        if participant.kind == sip_kind:
            return participant.identity
    return None


async def transfer_to_rep(
    room: Any, resolution: TransferResolution, settings: Settings
) -> str:
    """Hand the caller to the rep in `resolution`. Returns the number dialled.

    Raises `TransferError` for anything that stops the hand-off — no dialable
    number, no SIP leg in the room, or LiveKit refusing the REFER — so the caller
    is told the truth instead of being left listening to nothing.
    """
    rep = resolution.rep
    target = dial_target(rep)
    if target is None:
        raise TransferError(
            "no dialable number for the rep taking this call "
            f"({rep.name if rep else 'nobody resolved'})")

    identity = caller_identity(room)
    if identity is None:
        raise TransferError(
            "no SIP participant in the room, so there is no line to transfer")

    from livekit import api

    lkapi = api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    try:
        await lkapi.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=room.name,
                participant_identity=identity,
                transfer_to=target,
                play_dialtone=True,
            ),
            timeout=settings.sip_transfer_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - every failure here is the same failure
        raise TransferError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        await lkapi.aclose()

    logger.info("transferred the caller to %s on %s",
                rep.name or rep.rep_id, rep.spoken_phone)
    return target
