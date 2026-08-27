"""Vocal-delivery scoring: the acoustics arithmetic and the audio judge's frame.

The acoustics are the part a manager can trust week over week, so they are
tested as arithmetic: synthetic WAVs with known silence and amplitude produce
known pause ratios, hesitation and energy variation, forever. The delivery
judge's model is played by `httpx.MockTransport` speaking the real wire shape —
what's on trial is the request (clips as base64 input_audio parts), the
clamping, and the rule that a broken verdict lands as `delivery_error`, never
anywhere near the rep's end-of-call response. The manager tests pin the clip
lifecycle: recordings exist only until scoring unless the desk opted in.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from lanevoice.db.database import Database
from lanevoice.practice import delivery as delivery_mod
from lanevoice.practice.acoustics import analyse_clips
from lanevoice.practice.delivery import DELIVERY_RUBRIC, DeliveryJudge
from lanevoice.practice.recording import RECORDING_NAME, stitch_session
from lanevoice.practice.sessions import PracticeSessionManager
from lanevoice.practice.store import PracticeStore
from lanevoice.settings import get_settings

RATE = 16000


def _write_wav(path: Path, segments: list[tuple[float, float]],
               rate: int = RATE) -> Path:
    """A WAV built from (seconds, amplitude) segments — silence is amp 0.0."""
    audio = np.concatenate([np.full(int(rate * secs), amp, dtype=np.float32)
                            for secs, amp in segments])
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((audio * 32767).astype("<i2").tobytes())
    return path


# ---------------------------------------------------------------- acoustics #
def test_leading_silence_reads_as_hesitation_and_counts_toward_pauses(tmp_path):
    clip = _write_wav(tmp_path / "t.wav", [(1.0, 0.0), (1.0, 0.5)])
    m = analyse_clips([clip])
    assert m["leading_hesitation_secs"] == pytest.approx(1.0, abs=0.1)
    assert m["pause_ratio"] == pytest.approx(0.5, abs=0.05)


def test_a_mid_turn_stall_counts_as_a_long_pause(tmp_path):
    clip = _write_wav(tmp_path / "t.wav",
                      [(0.5, 0.5), (2.0, 0.0), (0.5, 0.5)])
    assert analyse_clips([clip])["long_pauses"] == 1


def test_trailing_silence_is_button_mechanics_not_a_stall(tmp_path):
    # Dead air after the last word is the rep releasing the button late.
    clip = _write_wav(tmp_path / "t.wav", [(0.5, 0.5), (3.0, 0.0)])
    assert analyse_clips([clip])["long_pauses"] == 0


def test_energy_variation_separates_monotone_from_lively(tmp_path):
    flat = _write_wav(tmp_path / "flat.wav", [(2.0, 0.5)])
    lively = _write_wav(tmp_path / "lively.wav",
                        [(0.5, 0.3), (0.5, 0.9), (0.5, 0.3), (0.5, 0.9)])
    assert analyse_clips([flat])["energy_variation"] < 0.05
    assert analyse_clips([lively])["energy_variation"] > 0.3


def test_non_wav_clips_are_skipped_never_an_error(tmp_path):
    webm = tmp_path / "t.webm"
    webm.write_bytes(b"\x1aEnot really audio")
    assert analyse_clips([webm]) is None


# ------------------------------------------------------------ deliveryjudge #
def _verdict(**overrides) -> dict:
    body = {"scores": {k: {"score": 7, "comment": f"heard {k}"}
                       for k in DELIVERY_RUBRIC},
            "coaching": ["slow the opening line down"]}
    body.update(overrides)
    return body


def _judge(handler, **settings_overrides) -> DeliveryJudge:
    settings = get_settings().model_copy(update={
        "openrouter_api_key": "test-key",
        "practice_delivery_model": "test/audio-judge", **settings_overrides})
    return DeliveryJudge(settings, transport=httpx.MockTransport(handler))


def test_the_judge_sends_clips_as_input_audio_and_normalises_the_verdict(tmp_path):
    clips = [_write_wav(tmp_path / f"turn_{i}.wav", [(1.0, 0.5)]) for i in range(2)]
    seen = {}

    def handle(request):
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(_verdict())}}]})

    verdict = _judge(handle).score(clips)
    assert seen["model"] == "test/audio-judge"
    parts = seen["messages"][0]["content"]
    audio_parts = [p for p in parts if p["type"] == "input_audio"]
    assert len(audio_parts) == 2
    assert all(p["input_audio"]["format"] == "wav" for p in audio_parts)
    assert verdict["overall"] == 7.0
    assert verdict["judged_secs"] == pytest.approx(2.0)
    assert verdict["scores"]["clarity"]["comment"] == "heard clarity"
    assert verdict["coaching"] == ["slow the opening line down"]


def test_out_of_scale_scores_are_clamped(tmp_path):
    clip = _write_wav(tmp_path / "t.wav", [(1.0, 0.5)])
    scores = {k: {"score": 7, "comment": ""} for k in DELIVERY_RUBRIC}
    scores["energy"]["score"] = 14

    def handle(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(_verdict(scores=scores))}}]})

    assert _judge(handle).score([clip])["scores"]["energy"]["score"] == 10


def test_the_clip_budget_is_front_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery_mod, "_MAX_JUDGED_SECS", 1.5)
    clips = [_write_wav(tmp_path / f"turn_{i}.wav", [(1.0, 0.5)]) for i in range(3)]
    seen = {}

    def handle(request):
        seen.update(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(_verdict())}}]})

    _judge(handle).score(clips)
    audio_parts = [p for p in seen["messages"][0]["content"]
                   if p["type"] == "input_audio"]
    assert len(audio_parts) == 1              # opening turn first, budget spent


def test_an_unparseable_verdict_degrades_to_delivery_error(tmp_path):
    clip = _write_wav(tmp_path / "t.wav", [(1.0, 0.5)])

    def handle(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "The rep sounded confident overall, I'd say."}}]})

    verdict = _judge(handle).score([clip])
    assert "delivery_error" in verdict and "raw" in verdict


def test_no_judgeable_clips_is_an_error_result_not_a_model_call(tmp_path):
    webm = tmp_path / "t.webm"
    webm.write_bytes(b"\x1aE")

    def handle(request):
        raise AssertionError("no model call should be made without wav clips")

    assert "delivery_error" in _judge(handle).score([webm])


def test_the_judge_refuses_to_build_unconfigured():
    with pytest.raises(RuntimeError, match="PRACTICE_DELIVERY_MODEL"):
        DeliveryJudge(get_settings().model_copy(
            update={"openrouter_api_key": "k", "practice_delivery_model": ""}))
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        DeliveryJudge(get_settings().model_copy(
            update={"openrouter_api_key": "",
                    "practice_delivery_model": "test/audio-judge"}))


# ---------------------------------------------------------------- stitching #
def test_stitching_joins_both_sides_resampled_to_one_rate(tmp_path):
    customer = _write_wav(tmp_path / "000_customer.wav", [(1.0, 0.5)], rate=24000)
    rep = _write_wav(tmp_path / "001_rep.wav", [(1.0, 0.5)], rate=16000)
    path = stitch_session(tmp_path, [customer, rep])
    assert path is not None and path.name == RECORDING_NAME
    with wave.open(str(path)) as wav:
        assert wav.getframerate() == 24000
        # two 1s clips + a 0.25s gap after each, all at the target rate
        assert wav.getnframes() == pytest.approx(2.5 * 24000, rel=0.02)


def test_stitching_with_nothing_readable_produces_no_recording(tmp_path):
    junk = tmp_path / "000_customer.wav"
    junk.write_bytes(b"RIFF-not-really")
    assert stitch_session(tmp_path, [junk]) is None
    assert not (tmp_path / RECORDING_NAME).exists()


# -------------------------------------------------------- manager lifecycle #
def _tts_wav() -> bytes:
    import io
    buf = io.BytesIO()
    audio = np.full(24000, 0.4, dtype=np.float32)      # 1s at the TTS rate
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes((audio * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class _FakeSpeech:
    def transcribe(self, audio: bytes, mime: str) -> str:
        return "Hey Dale, quick question about coverage."

    def synthesize(self, text: str) -> tuple[bytes, str, float]:
        return _tts_wav(), "audio/wav", 1.0


class _FakeDelivery:
    def __init__(self):
        self.scored: list[list[Path]] = []

    def score(self, clip_paths):
        self.scored.append(list(clip_paths))
        return {"overall": 8.0,
                "scores": {k: {"score": 8, "comment": "clear"} for k in DELIVERY_RUBRIC},
                "coaching": ["keep the pace"], "judged_secs": 2.0,
                "model": "fake/delivery"}


def _script(*lines):
    queue = list(lines)

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        assert queue, "the customer script ran out of lines"
        return queue.pop(0)

    return chat


def _fake_judge(settings):
    from lanevoice.practice.judge import FOCUS_KEY, RUBRIC

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        return json.dumps({"scores": {k: {"score": 6, "quote": "q", "comment": "c"}
                                      for k in [*RUBRIC, FOCUS_KEY]},
                           "win_condition_met": False, "win_evidence": "",
                           "strengths": [], "improvements": [], "summary": "ok"})

    return chat


def _voice_manager(tmp_path, keep_audio=False):
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    fake_delivery = _FakeDelivery()
    settings = get_settings().model_copy(update={"practice_keep_audio": keep_audio})
    mgr = PracticeSessionManager(
        db, settings,
        chat_factory=lambda s: _script("We're covered.", "Sure."),
        speech_factory=lambda s: _FakeSpeech(),
        judge_factory=_fake_judge,
        delivery_factory=lambda s: fake_delivery)
    return mgr, PracticeStore(db), fake_delivery, tmp_path / "practice_audio"


def _wav_clip() -> bytes:
    audio = np.full(RATE, 0.5, dtype=np.float32)
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((audio * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_voice_clips_live_exactly_until_scoring(tmp_path):
    mgr, store, fake_delivery, audio_dir = _voice_manager(tmp_path)
    started = mgr.start("brush_off", "Jordan", voice=True)
    mgr.turn_voice(started["session_id"], _wav_clip(), "audio/wav", 1.0)
    clip_dir = audio_dir / started["session_id"]
    # On disk during the session: customer opening, rep turn, customer reply.
    assert len(list(clip_dir.glob("*.wav"))) == 3

    summary = mgr.end(started["session_id"])
    # The raw turn clips are gone the moment it's scored; the stitched call
    # recording SURVIVES — it's what the play button serves.
    assert [p.name for p in clip_dir.glob("*")] == [RECORDING_NAME]
    assert mgr.recording_path(started["session_id"]) is not None
    # The delivery judge heard the REP only — never the customer's clips.
    assert fake_delivery.scored and len(fake_delivery.scored[0]) == 1
    assert all("_rep" in p.name for p in fake_delivery.scored[0])

    report = summary["report"]
    assert report["delivery"]["overall"] == 8.0
    assert report["metrics"]["pause_ratio"] is not None   # acoustics landed
    detail = mgr.report_detail(started["session_id"])
    assert detail["report"]["delivery"]["scores"]["warmth"]["score"] == 8
    assert detail["has_recording"] is True


def test_keep_audio_keeps_the_clips(tmp_path):
    mgr, _, _, audio_dir = _voice_manager(tmp_path, keep_audio=True)
    started = mgr.start("brush_off", "Jordan", voice=True)
    mgr.turn_voice(started["session_id"], _wav_clip(), "audio/wav", 1.0)
    mgr.end(started["session_id"])
    names = [p.name for p in (audio_dir / started["session_id"]).glob("*")]
    assert any("_rep" in n for n in names)            # raw clips retained
    assert RECORDING_NAME in names


def test_an_abandoned_session_leaves_no_recordings_behind(tmp_path):
    mgr, _, fake_delivery, audio_dir = _voice_manager(tmp_path)
    started = mgr.start("brush_off", "Jordan", voice=True)
    mgr.turn_voice(started["session_id"], _wav_clip(), "audio/wav", 1.0)
    mgr.abandon(started["session_id"])
    assert not (audio_dir / started["session_id"]).exists()
    assert fake_delivery.scored == []                 # abandoned is never judged


def test_text_sessions_never_touch_the_delivery_judge(tmp_path):
    mgr, store, fake_delivery, _ = _voice_manager(tmp_path)
    started = mgr.start("brush_off", "Jordan")        # text mode
    mgr.turn(started["session_id"], "Quick question —")
    summary = mgr.end(started["session_id"])
    assert fake_delivery.scored == []
    assert "delivery" not in summary["report"]
    assert store.report_detail(started["session_id"])["report"]["delivery"] is None
    detail = mgr.report_detail(started["session_id"])
    assert detail["has_recording"] is False
    assert mgr.recording_path(started["session_id"]) is None
