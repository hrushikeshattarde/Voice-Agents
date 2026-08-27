"""Practice sessions: the state machine around the persona, without a model.

The customer's lines are scripted through the same ChatFn contract the real
provider path satisfies, so these tests assert on what the MANAGER decided —
done, end_reason, what was persisted — never on prose. What would break in
production without them: a hang-up token reaching a rep's screen, a runaway
session billing model calls forever, a closed tab losing its transcript, or
practice quietly "working" against the offline stub and teaching nothing.
"""

from __future__ import annotations

import json

import pytest

from lanevoice.db.database import Database
from lanevoice.practice.judge import FOCUS_KEY, RUBRIC
from lanevoice.practice.persona import HANGUP_TOKEN, build_persona_chat
from lanevoice.practice.sessions import _MAX_SESSIONS, PracticeSessionManager
from lanevoice.practice.store import PracticeStore
from lanevoice.settings import get_settings


def _script(*lines):
    """A ChatFn playing the customer from a fixed list of lines. Asserts inside
    itself, like the suite's other fakes: running dry is a test bug, not a model
    quirk to degrade around."""
    queue = list(lines)

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        assert queue, "the customer script ran out of lines"
        return queue.pop(0)

    return chat


def _fake_judge(settings):
    """A judge returning one fixed, valid verdict. EVERY manager these tests
    build must inject a judge: the default factory builds a real provider
    client, and on a developer machine with a filled-in `.env` a finished
    session would silently ship its transcript to a real model mid-test."""

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        scores = {k: {"score": 6, "quote": "q", "comment": "c"}
                  for k in [*RUBRIC, FOCUS_KEY]}
        return json.dumps({"scores": scores, "win_condition_met": False,
                           "win_evidence": "", "strengths": ["kept it brief"],
                           "improvements": [], "summary": "Solid work."})

    return chat


def _manager(tmp_path, *lines, max_turns: int = 40):
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    settings = get_settings().model_copy(update={"practice_max_turns": max_turns})
    mgr = PracticeSessionManager(db, settings, chat_factory=lambda s: _script(*lines),
                                 judge_factory=_fake_judge)
    return mgr, PracticeStore(db)


# ---------------------------------------------------------------- lifecycle #
def test_a_session_opens_with_the_profile_line_and_persists_from_turn_one(tmp_path):
    mgr, store = _manager(tmp_path, "We're all set, but go ahead.")
    started = mgr.start("brush_off", "Jordan")
    assert started["profile"]["id"] == "brush_off"
    assert started["opening"]        # the profile's verbatim opening
    assert started["max_turns"] == 40

    # The opening is already on disk before the rep says a word — a tab closed
    # here still leaves a coherent record.
    row = store.session(started["session_id"])
    assert row["status"] == "active"
    assert row["transcript"] == [["customer", started["opening"]]]

    res = mgr.turn(started["session_id"], "Hi Dale, this is Jordan with Circle.")
    assert res["done"] is False
    assert res["turns"] == 1
    row = store.session(started["session_id"])
    assert row["turns"] == 1
    assert row["transcript"][-2:] == [
        ["rep", "Hi Dale, this is Jordan with Circle."],
        ["customer", "We're all set, but go ahead."],
    ]


def test_the_customer_hanging_up_ends_and_finalizes_the_session(tmp_path):
    mgr, store = _manager(tmp_path, f"Gotta run. Good luck out there. {HANGUP_TOKEN}")
    started = mgr.start("brush_off", "Jordan")
    res = mgr.turn(started["session_id"], "Do you ever hit overflow at quarter-end?")
    assert res["done"] is True
    assert res["end_reason"] == "hangup"
    assert res["summary"]["turns"] == 1
    # The token is control flow, not dialogue — neither the screen nor the
    # stored transcript may carry it.
    assert HANGUP_TOKEN not in res["reply"]
    row = store.session(started["session_id"])
    assert row["status"] == "done"
    assert row["end_reason"] == "hangup"
    assert HANGUP_TOKEN not in str(row["transcript"])
    # A finished session is forgotten.
    with pytest.raises(KeyError):
        mgr.turn(started["session_id"], "hello?")


def test_the_turn_cap_ends_a_runaway_session(tmp_path):
    mgr, store = _manager(tmp_path, "Uh huh.", "Sure.", "Still here.", max_turns=2)
    started = mgr.start("chatty_noncommitter", "Jordan")
    assert mgr.turn(started["session_id"], "one")["done"] is False
    res = mgr.turn(started["session_id"], "two")
    assert res["done"] is True
    assert res["end_reason"] == "turn_limit"
    assert store.session(started["session_id"])["end_reason"] == "turn_limit"


def test_the_rep_ending_the_call_finalizes_with_a_summary(tmp_path):
    mgr, store = _manager(tmp_path, "We're covered.")
    started = mgr.start("brush_off", "Jordan")
    mgr.turn(started["session_id"], "Quick question about your flatbed coverage —")
    summary = mgr.end(started["session_id"])
    assert summary["end_reason"] == "ended"
    assert summary["turns"] == 1
    assert summary["profile_name"] == "The Brush-off"
    assert store.session(started["session_id"])["status"] == "done"
    with pytest.raises(KeyError):
        mgr.end(started["session_id"])


def test_abandon_is_quiet_and_recorded(tmp_path):
    mgr, store = _manager(tmp_path)
    started = mgr.start("brush_off", "Jordan")
    assert mgr.abandon(started["session_id"]) is True
    assert mgr.abandon(started["session_id"]) is False   # retry after expiry: no error
    assert store.session(started["session_id"])["end_reason"] == "abandoned"


def test_an_unknown_profile_is_refused_by_name(tmp_path):
    mgr, _ = _manager(tmp_path)
    with pytest.raises(ValueError, match="angry_martian"):
        mgr.start("angry_martian", "Jordan")


def test_the_session_cap_holds(tmp_path):
    mgr, _ = _manager(tmp_path)
    for _ in range(_MAX_SESSIONS):
        mgr.start("brush_off", "Jordan")
    with pytest.raises(RuntimeError, match="Too many open practice sessions"):
        mgr.start("brush_off", "Jordan")


# ------------------------------------------------------------------ persona #
def test_an_empty_model_reply_raises_and_leaves_the_session_retryable(tmp_path):
    mgr, store = _manager(tmp_path, "", "Prairie Steel, still here.")
    started = mgr.start("brush_off", "Jordan")
    with pytest.raises(RuntimeError, match="send the turn again"):
        mgr.turn(started["session_id"], "Hello?")
    # The failed turn left no half-exchange behind; the retry lands cleanly.
    res = mgr.turn(started["session_id"], "Hello? You there?")
    assert res["done"] is False
    assert store.session(started["session_id"])["turns"] == 1


def test_the_persona_prompt_carries_the_profile_material(tmp_path):
    """Wiring, not wording: the profile's private material must actually reach
    the model's system prompt, or the customer has no insides to discover."""
    seen = {}

    def capture(system: str, user: str, *, max_tokens: int) -> str:
        seen["system"], seen["user"] = system, user
        return "Yeah, we're covered."

    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    mgr = PracticeSessionManager(db, get_settings(), chat_factory=lambda s: capture,
                                 judge_factory=_fake_judge)
    started = mgr.start("brush_off", "Jordan")
    mgr.turn(started["session_id"], "How do you handle quarter-end overflow?")

    profile = mgr._profiles["brush_off"]
    for fact in profile.hidden_facts:
        assert fact in seen["system"]
    assert profile.win_condition in seen["system"]
    assert HANGUP_TOKEN in seen["system"]         # or the customer can never leave
    assert "quarter-end overflow" in seen["user"]  # the rep's turn reached the model


# -------------------------------------------------------------------- voice #
class _FakeSpeech:
    """Satisfies the PracticeSpeech surface with fixed, measurable audio."""

    def __init__(self, fail_tts: bool = False):
        self.fail_tts = fail_tts

    def transcribe(self, audio: bytes, mime: str) -> str:
        assert audio, "an empty clip should have been rejected before STT"
        return "Hi Dale, quick question about your flatbed coverage."

    def synthesize(self, text: str) -> tuple[bytes, str, float]:
        if self.fail_tts:
            raise RuntimeError("voice model down")
        return b"RIFF-fake-wav", "audio/wav", 2.5


def _voice_manager(tmp_path, *lines, fail_tts: bool = False):
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    # Delivery judging is off here — these tests own the speech/turn mechanics;
    # test_practice_delivery.py owns the vocal verdict.
    settings = get_settings().model_copy(update={"practice_delivery_model": ""})
    mgr = PracticeSessionManager(
        db, settings,
        chat_factory=lambda s: _script(*lines),
        speech_factory=lambda s: _FakeSpeech(fail_tts=fail_tts),
        judge_factory=_fake_judge)
    return mgr, PracticeStore(db)


def test_a_voice_session_carries_audio_and_talk_time(tmp_path):
    mgr, store = _voice_manager(tmp_path, "We're covered. What's this about?")
    started = mgr.start("brush_off", "Jordan", voice=True)
    assert started["mode"] == "voice"
    assert started["audio"]                     # the opening is spoken too
    assert started["audio_mime"] == "audio/wav"

    res = mgr.turn_voice(started["session_id"], b"opus-bytes", "audio/webm", 6.4)
    # What STT heard is what the persona answered — and what the rep sees.
    assert res["heard"].startswith("Hi Dale")
    assert res["audio"] and res["audio_mime"] == "audio/wav"

    summary = mgr.end(started["session_id"])
    assert summary["mode"] == "voice"
    row = store.session(started["session_id"])
    assert row["mode"] == "voice"
    assert row["rep_audio_secs"] == pytest.approx(6.4)
    # Opening (2.5s) + one reply (2.5s), both exact from synthesis.
    assert row["customer_audio_secs"] == pytest.approx(5.0)
    # The transcript stores the transcription, so the phase-2 judge reads a
    # voice call exactly like a text one.
    assert row["transcript"][1][0] == "rep"
    assert row["transcript"][1][1].startswith("Hi Dale")


def test_a_tts_failure_mid_session_degrades_to_text_not_a_500(tmp_path):
    mgr, store = _voice_manager(tmp_path, "Still here.", fail_tts=True)
    # Start would synthesize the opening and fail — that is the honest outcome
    # for a voice session that can't speak at all.
    with pytest.raises(RuntimeError, match="voice model down"):
        mgr.start("brush_off", "Jordan", voice=True)


def test_a_reply_tts_failure_keeps_the_words(tmp_path):
    """The persona already spoke (and was billed for) the turn — a broken voice
    must not eat the words."""
    speech = _FakeSpeech()
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    settings = get_settings().model_copy(update={"practice_delivery_model": ""})
    mgr = PracticeSessionManager(db, settings,
                                 chat_factory=lambda s: _script("Make it quick."),
                                 speech_factory=lambda s: speech,
                                 judge_factory=_fake_judge)
    started = mgr.start("brush_off", "Jordan", voice=True)
    speech.fail_tts = True                      # the voice dies AFTER the start
    res = mgr.turn_voice(started["session_id"], b"opus-bytes", "audio/webm", 3.0)
    assert res["reply"] == "Make it quick."
    assert "audio" not in res
    assert "voice model down" in res["audio_error"]
    row = PracticeStore(db).session(started["session_id"])
    assert row["turns"] == 1                    # the exchange still persisted


def test_an_absurd_client_duration_is_clamped_not_trusted(tmp_path):
    mgr, store = _voice_manager(tmp_path, "Uh huh.")
    started = mgr.start("brush_off", "Jordan", voice=True)
    mgr.turn_voice(started["session_id"], b"opus-bytes", "audio/webm", 99999.0)
    mgr.end(started["session_id"])
    assert store.session(started["session_id"])["rep_audio_secs"] <= 300.0


# ------------------------------------------------------------- offline rule #
def test_practice_refuses_the_offline_stub_and_names_the_fix():
    """USE_LLM=false is a fine way to run the playground — the sales agent's
    tests assert on its state machine. A practice CUSTOMER with no model is a
    conversation with nobody, so it must refuse loudly, naming the setting."""
    offline = get_settings().model_copy(update={"use_llm": False})
    with pytest.raises(RuntimeError, match="USE_LLM"):
        build_persona_chat(offline)

    keyless = get_settings().model_copy(
        update={"use_llm": True, "llm_provider": "openrouter", "openrouter_api_key": ""})
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_persona_chat(keyless)
