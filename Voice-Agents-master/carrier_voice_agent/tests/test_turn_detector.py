"""The session waits for the rest of a sentence instead of answering a pause.

Until 2026-09-08 no turn detector was wired into the session, so the framework
committed every caller turn MIN_ENDPOINTING_DELAY after the VAD heard silence,
whatever the words were, and MAX_ENDPOINTING_DELAY never applied. The 09-04
calls show it: "Looking for" answered as a whole turn, and one answer to the
empty-call question split into three, each met with "didn't catch that". These
pin the wiring: the detector is built from settings, it is the hosted model and
not the local fallback, and the session actually receives it.
"""

from __future__ import annotations

from livekit.agents import inference

from lanevoice.settings import get_settings
from lanevoice.telephony.worker import build_turn_detector, session_kwargs


def _settings(**overrides):
    # The hosted detector refuses to build without LiveKit credentials; a live
    # worker has them (the STT and TTS need the same ones), a test supplies fakes.
    return get_settings().model_copy(update={
        "livekit_api_key": "APIfake", "livekit_api_secret": "secretfake", **overrides})


def test_the_hosted_detector_is_built_when_enabled():
    detector = build_turn_detector(_settings(turn_detector_enabled=True))
    assert isinstance(detector, inference.TurnDetector)
    assert detector.model == "turn-detector-v1"        # hosted, not the local mini model


def test_it_is_on_by_default_and_the_switch_turns_it_off():
    assert get_settings().turn_detector_enabled is True
    assert build_turn_detector(_settings(turn_detector_enabled=False)) is None


def test_the_session_receives_the_detector_the_worker_built():
    settings = _settings(stt_provider="inference")
    detector = build_turn_detector(settings)
    kwargs = session_kwargs({"turn_detector": detector, "keyterms": ["load"]}, settings)
    # INSIDE turn_handling. As its own `turn_detection=` argument beside
    # turn_handling= the framework ignores it and builds a default detector
    # (the local mini model outside dev mode) — verified on 1.6.6, and how the
    # worker ran from 09-08 to 09-09.
    assert kwargs["turn_handling"]["turn_detection"] is detector
    assert "turn_detection" not in kwargs
    assert kwargs["turn_handling"]["endpointing"]["min_delay"] == settings.min_endpointing_delay
    assert kwargs["stt_context_options"]["keyterms"] == ["load"]      # untouched by the split

    # Off = an EXPLICIT None: the framework's documented switch. Leaving the key
    # out means "use the default detector", which is not off at all.
    off = session_kwargs({"turn_detector": None, "keyterms": ["load"]}, settings)
    assert off["turn_handling"]["turn_detection"] is None
