"""
Deterministic acoustics — what the rep's audio shows, computed, not opined.

The vocal-delivery judge (`delivery.py`) hears tone and warmth, but like any
model its scores carry some run-to-run drift. These numbers don't: pauses,
hesitation and energy variation are arithmetic over the recorded WAV frames,
so "leading hesitation down from 1.8s to 0.3s" is a fact a rep can train
against week over week — the same split the conversation side already has
between the judge's rubric and `metrics.py`.

WAV only, deliberately. The browser records WAV precisely so this module can
read it with soundfile+numpy (already dependencies); teaching it webm would
mean shipping ffmpeg. A non-WAV clip is skipped, never an error.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from lanevoice.logging_config import get_logger

logger = get_logger(__name__)

_FRAME_SECS = 0.03          # 30ms frames — standard speech-analysis granularity
# A frame is "silent" below this fraction of the clip's loud reference (the
# 95th-percentile frame RMS). Relative, not absolute, so a quiet mic and a hot
# mic measure the same speech the same way.
_SILENCE_FRACTION = 0.10
_LONG_PAUSE_SECS = 1.5      # a mid-turn gap this long is a stall, not a breath


def analyse_clips(paths: list[Path]) -> dict | None:
    """Aggregate acoustics across a session's rep clips, or None when there is
    nothing readable to measure (text session, non-WAV clips, empty files)."""
    voiced_rms: list[np.ndarray] = []
    total_frames = 0
    silent_frames = 0
    long_pauses = 0
    leading = []
    for path in paths:
        clip = _read_wav(path)
        if clip is None:
            continue
        audio, rate = clip
        frame = max(1, int(rate * _FRAME_SECS))
        usable = (len(audio) // frame) * frame
        if not usable:
            continue
        rms = np.sqrt((audio[:usable].reshape(-1, frame) ** 2).mean(axis=1))
        loud = np.percentile(rms, 95)
        if loud <= 0:
            continue                     # digital silence end to end
        silent = rms < loud * _SILENCE_FRACTION
        total_frames += len(rms)
        silent_frames += int(silent.sum())
        voiced_rms.append(rms[~silent])
        leading.append(_leading_run(silent) * _FRAME_SECS)
        long_pauses += _long_runs(silent, int(_LONG_PAUSE_SECS / _FRAME_SECS))
    if not total_frames:
        return None
    voiced = np.concatenate(voiced_rms) if voiced_rms else np.array([])
    # Coefficient of variation of voiced loudness: a flat, monotone delivery
    # sits low; a lively one varies. A proxy, not a verdict — the delivery
    # judge carries the verdict.
    energy_variation = (round(float(voiced.std() / voiced.mean()), 2)
                        if voiced.size and voiced.mean() > 0 else None)
    return {
        "pause_ratio": round(silent_frames / total_frames, 2),
        "long_pauses": long_pauses,
        "leading_hesitation_secs": round(float(np.mean(leading)), 1) if leading else None,
        "energy_variation": energy_variation,
    }


def _read_wav(path: Path) -> tuple[np.ndarray, int] | None:
    if path.suffix.lower() != ".wav":
        return None
    try:
        import soundfile as sf
        audio, rate = sf.read(str(path), dtype="float32")
    except Exception as exc:  # noqa: BLE001 - a bad clip skips, never breaks scoring
        logger.warning("Skipping unreadable practice clip %s: %s", path.name, exc)
        return None
    if getattr(audio, "ndim", 1) > 1:
        audio = audio.mean(axis=1)
    return (audio, rate) if len(audio) else None


def _leading_run(silent: np.ndarray) -> int:
    """Frames of silence before the first voiced frame — the pre-speech stall."""
    voiced = np.flatnonzero(~silent)
    return int(voiced[0]) if voiced.size else len(silent)


def _long_runs(silent: np.ndarray, min_frames: int) -> int:
    """Contiguous silent runs at least `min_frames` long, excluding the clip's
    leading and trailing silence (dead air around the press-and-release is
    button mechanics, not a conversational stall)."""
    voiced = np.flatnonzero(~silent)
    if voiced.size < 2:
        return 0
    inner = silent[voiced[0]:voiced[-1] + 1]
    runs, current = 0, 0
    for is_silent in inner:
        current = current + 1 if is_silent else 0
        if current == min_frames:        # count each run once, as it crosses
            runs += 1
    return runs
