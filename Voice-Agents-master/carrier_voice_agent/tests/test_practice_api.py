"""The practice HTTP surface, exercised over a real socket.

Same pattern as the dashboard's server test: `serve()` on an ephemeral port,
stdlib urllib against it, so the routing regexes, status codes and error
mapping are the real ones. Two contracts matter most here: the profile cards
must never leak the answer key to the browser, and a misconfigured desk must
get a 400 that names the setting to fill — not a stack trace, not a silent
stub customer.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from lanevoice.db.database import Database
from lanevoice.practice.persona import HANGUP_TOKEN
from lanevoice.practice.sessions import PracticeSessionManager
from lanevoice.settings import get_settings


@pytest.fixture
def offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "practice-api.db"))
    monkeypatch.setenv("USE_LLM", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _tiny_wav(rate: int = 16000, secs: float = 0.5) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x10\x00" * int(rate * secs))
    return buf.getvalue()


class _FakeSpeech:
    """Fixed audio in both directions, so the HTTP layer is what's on trial.
    The synthesized side is a REAL (if silent-ish) WAV so the call recording
    can actually be stitched and served."""

    def transcribe(self, audio: bytes, mime: str) -> str:
        assert audio
        return "Hi, quick question about your coverage."

    def synthesize(self, text: str) -> tuple[bytes, str, float]:
        return _tiny_wav(), "audio/wav", 2.0


@pytest.fixture
def server(offline_env):
    """A live dashboard server whose practice manager plays a scripted customer."""
    from lanevoice.dashboard.server import DashboardApp, serve

    app = DashboardApp()

    def scripted_chat(settings):
        lines = ["We're all set, but you've got thirty seconds.",
                 f"Alright — call me next quarter. {HANGUP_TOKEN}"]

        def chat(system: str, user: str, *, max_tokens: int) -> str:
            assert lines, "the customer script ran out of lines"
            return lines.pop(0)

        return chat

    def fake_judge(settings):
        from lanevoice.practice.judge import FOCUS_KEY, RUBRIC

        def chat(system: str, user: str, *, max_tokens: int) -> str:
            scores = {k: {"score": 7, "quote": "thirty seconds", "comment": "fine"}
                      for k in [*RUBRIC, FOCUS_KEY]}
            return json.dumps({"scores": scores, "win_condition_met": True,
                               "win_evidence": "callback agreed",
                               "strengths": ["brevity"], "improvements": [],
                               "summary": "Good call."})

        return chat

    app.practice = PracticeSessionManager(
        Database(get_settings().db_path),
        get_settings().model_copy(update={"practice_delivery_model": ""}),
        chat_factory=scripted_chat, speech_factory=lambda s: _FakeSpeech(),
        judge_factory=fake_judge)

    httpd = serve("127.0.0.1", 0, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path) as res:
        return res.status, json.loads(res.read())


def _send(base, path, body=None, method="POST"):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as res:
        return res.status, json.loads(res.read())


def _error(base, path, body=None, method="POST"):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _send(base, path, body, method)
    return exc.value.code, json.loads(exc.value.read())["error"]


# ----------------------------------------------------------------- profiles #
def test_the_profiles_route_serves_cards_without_the_answer_key(server):
    status, data = _get(server, "/api/practice/profiles")
    assert status == 200
    assert len(data["profiles"]) == 8
    flat = json.dumps(data).lower()
    for leak in ("hidden_facts", "hangup_triggers", "warms_to", "objections",
                 "speech_style", "whitfield", "rfp", "roofing"):  # earned secrets
        assert leak not in flat


# ---------------------------------------------------------------- roundtrip #
def test_a_practice_session_runs_end_to_end_over_http(server):
    status, started = _send(server, "/api/practice/sessions",
                            {"profile_id": "brush_off", "rep_name": "Jordan"})
    assert status == 201
    assert started["opening"]
    sid = started["session_id"]

    _, res = _send(server, f"/api/practice/sessions/{sid}/turns",
                   {"text": "Hi Dale — thirty seconds, one question."})
    assert res["done"] is False
    assert res["reply"] == "We're all set, but you've got thirty seconds."

    _, res = _send(server, f"/api/practice/sessions/{sid}/turns",
                   {"text": "Who covers you when quarter-end overflows?"})
    assert res["done"] is True
    assert res["end_reason"] == "hangup"
    assert HANGUP_TOKEN not in res["reply"]
    assert res["summary"]["profile_id"] == "brush_off"

    # A finished session is gone: the turn route 404s, it doesn't resurrect.
    code, message = _error(server, f"/api/practice/sessions/{sid}/turns",
                           {"text": "hello?"})
    assert code == 404
    assert "session" in message


def test_the_rep_can_hang_up_and_get_the_summary(server):
    _, started = _send(server, "/api/practice/sessions",
                       {"profile_id": "rate_shopper", "rep_name": "Jordan"})
    sid = started["session_id"]
    status, res = _send(server, f"/api/practice/sessions/{sid}/end")
    assert status == 200
    assert res["summary"]["end_reason"] == "ended"
    assert res["summary"]["turns"] == 0


def test_abandon_via_delete_is_idempotent(server):
    _, started = _send(server, "/api/practice/sessions",
                       {"profile_id": "gatekeeper", "rep_name": "Jordan"})
    sid = started["session_id"]
    _, res = _send(server, f"/api/practice/sessions/{sid}", method="DELETE")
    assert res["ended"] is True
    _, res = _send(server, f"/api/practice/sessions/{sid}", method="DELETE")
    assert res["ended"] is False


# -------------------------------------------------------------------- voice #
def _send_audio(base, path, clip: bytes, secs: float, ctype: str = "audio/webm"):
    req = urllib.request.Request(
        base + path, method="POST", data=clip,
        headers={"Content-Type": ctype, "X-Audio-Seconds": f"{secs:.1f}"})
    with urllib.request.urlopen(req) as res:
        return res.status, json.loads(res.read())


def test_a_voice_turn_roundtrips_with_a_clip_bigger_than_the_json_cap(server):
    _, started = _send(server, "/api/practice/sessions",
                       {"profile_id": "brush_off", "rep_name": "Jordan",
                        "voice": True})
    assert started["mode"] == "voice"
    assert started["audio"]                     # opening line audio, base64 WAV

    # 200KB of "opus" — over the 64KB JSON cap on purpose: the audio route has
    # its own budget, because a fifteen-second clip is bigger than any JSON turn.
    status, res = _send_audio(
        server, f"/api/practice/sessions/{started['session_id']}/turns",
        b"\x1a" * 200_000, 12.5)
    assert status == 200
    assert res["heard"].startswith("Hi, quick question")
    assert res["reply"] == "We're all set, but you've got thirty seconds."
    assert res["audio"] and res["audio_mime"] == "audio/wav"


def test_the_call_recording_is_served_after_a_voice_session(server):
    _, started = _send(server, "/api/practice/sessions",
                       {"profile_id": "brush_off", "rep_name": "Jordan",
                        "voice": True})
    sid = started["session_id"]
    _send_audio(server, f"/api/practice/sessions/{sid}/turns",
                _tiny_wav(), 0.5, ctype="audio/wav")
    _send(server, f"/api/practice/sessions/{sid}/end")

    _, detail = _get(server, f"/api/practice/reports/{sid}")
    assert detail["has_recording"] is True
    with urllib.request.urlopen(
            f"{server}/api/practice/sessions/{sid}/recording") as res:
        assert res.status == 200
        assert res.headers["Content-Type"] == "audio/wav"
        assert res.read()[:4] == b"RIFF"

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{server}/api/practice/sessions/nope/recording")
    assert exc.value.code == 404


def test_json_turns_still_respect_the_small_body_cap(server):
    _, started = _send(server, "/api/practice/sessions",
                       {"profile_id": "brush_off", "rep_name": "Jordan"})
    code, message = _error(server,
                           f"/api/practice/sessions/{started['session_id']}/turns",
                           {"text": "x" * 100_000})
    assert code == 400
    assert "too large" in message


# ----------------------------------------------------------------- managers #
def test_the_managers_route_serves_the_roster_and_email_state(server):
    status, data = _get(server, "/api/practice/managers")
    assert status == 200
    # The shipped roster is examples-only, and the test env pins SMTP off.
    assert data["managers"] == []
    assert data["email_configured"] is False


# ------------------------------------------------------------------ reports #
def test_reports_are_listed_and_retrievable_after_a_session(server):
    _, started = _send(server, "/api/practice/sessions",
                       {"profile_id": "brush_off", "rep_name": "Jordan"})
    sid = started["session_id"]
    _send(server, f"/api/practice/sessions/{sid}/turns", {"text": "Quick question —"})
    _, ended = _send(server, f"/api/practice/sessions/{sid}/end")
    assert ended["summary"]["report"]["overall"] == 7.0

    status, listing = _get(server, "/api/practice/reports")
    assert status == 200
    row = next(r for r in listing["reports"] if r["session_id"] == sid)
    assert row["overall"] == 7.0
    assert row["win_condition_met"] is True

    status, detail = _get(server, f"/api/practice/reports/{sid}")
    assert status == 200
    assert detail["report"]["scores"]["closing"]["score"] == 7
    assert detail["report"]["metrics"]["rep_turns"] == 1
    assert detail["transcript"][0][0] == "customer"

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/practice/reports/nope")
    assert exc.value.code == 404


# ------------------------------------------------------------------- errors #
def test_missing_fields_are_a_400_that_names_the_field(server):
    code, message = _error(server, "/api/practice/sessions", {"rep_name": "Jordan"})
    assert code == 400 and "profile_id" in message
    code, message = _error(server, "/api/practice/sessions", {"profile_id": "brush_off"})
    assert code == 400 and "rep_name" in message


def test_an_unconfigured_desk_gets_a_400_naming_the_setting(offline_env):
    """The DEFAULT manager (no scripted chat) with USE_LLM=false: starting a
    session must answer with the setting to fill, not a 500 or a stub customer."""
    from lanevoice.dashboard.server import DashboardApp, serve

    httpd = serve("127.0.0.1", 0, DashboardApp())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, message = _error(base, "/api/practice/sessions",
                               {"profile_id": "brush_off", "rep_name": "Jordan"})
        assert code == 400
        assert "USE_LLM" in message
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
