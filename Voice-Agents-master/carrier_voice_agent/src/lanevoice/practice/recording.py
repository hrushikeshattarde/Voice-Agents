"""
The call recording — both sides of a voice session stitched into one WAV.

During the session the audio exists only as moments: the rep's clip goes up,
the customer's comes down, and each is heard once. Playback needs a single
artifact, so when a voice session is scored its clips are joined in speaking
order — customer opening, rep turn, customer reply, ... — into `call.wav`,
which is what the dashboard's player streams.

The two sides arrive at different rates (the browser records at 16 kHz, the
TTS answers at its own rate), so everything is resampled to one rate before
joining; linear interpolation is plenty for speech playback. A short gap
between clips stands in for the turn-taking silence the transport never
carried.

Retention: `call.wav` is the point of this module, so it is KEPT — it's the
conversation the rep can replay and a manager can listen to. The raw per-turn
clips remain governed by PRACTICE_KEEP_AUDIO. Deleting a session's recording
is deleting its file under practice_audio/.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from lanevoice.logging_config import get_logger
from lanevoice.practice.acoustics import _read_wav
from lanevoice.practice.speech import _to_wav

logger = get_logger(__name__)

RECORDING_NAME = "call.wav"
_TARGET_RATE = 24000        # the TTS side's usual rate; the rep side upsamples
_GAP_SECS = 0.25


def stitch_session(clip_dir: Path, ordered_clips: list[Path]) -> Path | None:
    """`call.wav` from the session's clips in speaking order, or None when
    nothing was readable. Best-effort by design: a bad clip is skipped, a
    failed stitch costs the replay, never the scorecard."""
    gap = np.zeros(int(_TARGET_RATE * _GAP_SECS), dtype=np.float32)
    pieces: list[np.ndarray] = []
    for path in ordered_clips:
        clip = _read_wav(path)
        if clip is None:
            continue
        audio, rate = clip
        if rate != _TARGET_RATE:
            n = max(1, int(len(audio) * _TARGET_RATE / rate))
            audio = np.interp(np.linspace(0, len(audio) - 1, n),
                              np.arange(len(audio)), audio).astype(np.float32)
        pieces.append(audio)
        pieces.append(gap)
    if not pieces:
        return None
    path = clip_dir / RECORDING_NAME
    try:
        path.write_bytes(_to_wav(np.concatenate(pieces), _TARGET_RATE))
    except OSError as exc:
        logger.warning("Could not write call recording %s: %s", path, exc)
        return None
    return path
