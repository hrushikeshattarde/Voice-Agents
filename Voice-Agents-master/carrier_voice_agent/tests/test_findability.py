"""Finding the call that went wrong, and where its seconds went, without a log.

Three things the dashboard could not answer on 09-09 and now can:

* Which calls had a line cut off, an answer go unheard, a backchannel dropped
  — typed events on the call, flagged on the Runs row and filterable.
* Where the seconds went in a slow turn — the caller's end-of-turn wait, the
  model's first text, the voice's first audio and the speech, stored beside
  each line of the transcript.
* Whether the phone worker is up at all, on which build, with which settings —
  its heartbeat row and the Overview's verdict on it.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.dashboard.queries import (
    SLOW_TURN_SECS,
    DashboardQueries,
    _transcript_with_timing,
)
from lanevoice.db import Database, Repository
from lanevoice.settings import get_settings
from lanevoice.telephony import worker
from lanevoice.voice import StubComposer


@pytest.fixture
def dash(tmp_path):
    db = Database(tmp_path / "dash.db")
    db.reset(seed=True)
    return Repository(db), DashboardQueries(db)


def _brain(repo, **overrides):
    settings = get_settings().model_copy(update=overrides) if overrides else get_settings()
    a = CarrierSalesAgent(repo, StubComposer(), settings=settings)
    a.greeting()
    return a


# --------------------------------------------------------------------------- #
# Events on the record, and flags on the row
# --------------------------------------------------------------------------- #
def test_an_event_is_typed_on_the_call_and_shows_as_a_flag(dash):
    repo, queries = dash
    a = _brain(repo)
    a.record_event("sip_wait", "the greeting waited 0.4s for the SIP leg", seconds=0.4)
    a.record_event("cut_off", "the caller cut in after 3 of 20 words")
    a.record_event("cut_off", "the caller cut in before any of 12 words played")

    row = queries.calls()[0]
    assert row["call_id"] == a.call_id
    assert row["flags"] == {"sip_wait": 1, "cut_off": 2}

    detail = queries.call_detail(a.call_id)
    assert [e["kind"] for e in detail["events"]] == ["sip_wait", "cut_off", "cut_off"]
    assert detail["events"][0]["data"] == {"seconds": 0.4}
    assert detail["flags"]["cut_off"] == 2


def test_the_runs_list_filters_by_flag(dash):
    repo, queries = dash
    cut = _brain(repo)
    cut.record_event("cut_off", "cut")
    quiet = _brain(repo)
    quiet.record_event("unheard", "nothing transcribed")
    clean = _brain(repo)

    assert {r["call_id"] for r in queries.calls(flag="cut_off")} == {cut.call_id}
    assert {r["call_id"] for r in queries.calls(flag="unheard")} == {quiet.call_id}
    assert {r["call_id"] for r in queries.calls()} >= {cut.call_id, quiet.call_id, clean.call_id}
    assert queries.calls(flag="compose_failed") == []


def test_the_brains_own_decisions_leave_events(dash):
    repo, queries = dash
    a = _brain(repo)
    a.handle("about L1001")
    a.note_unheard()
    reply = a.transcript[-1][1]
    a.note_playback_cut(reply, "")                     # cut before a word played
    kinds = [e["kind"] for e in queries.call_detail(a.call_id)["events"]]
    assert kinds == ["unheard", "cut_off"]
    cut = queries.call_detail(a.call_id)["events"][1]
    assert cut["data"]["heard_words"] == 0 and cut["data"]["composed_words"] > 0
    assert "before any of" in cut["detail"]


def test_a_partly_heard_line_is_marked_cut_on_its_clock(dash):
    repo, queries = dash
    a = _brain(repo)
    a.handle("about L1001")
    reply = a.transcript[-1][1]
    a.note_playback_cut(reply, reply.split()[0])       # one word of it played
    turns = queries.call_detail(a.call_id)["transcript"]
    assert turns[-1]["cut"] is True
    assert turns[-1]["text"].endswith("[cut off by the caller]")


# --------------------------------------------------------------------------- #
# The per-line clock
# --------------------------------------------------------------------------- #
def test_the_callers_wait_and_the_replys_making_land_beside_the_lines(dash):
    repo, queries = dash
    a = _brain(repo)
    a.handle("about L1001", heard_timing={"eou": 3.01, "stt": 0.5, "max": True})
    a.note_turn_voice(ttfb=0.18, speech=6.3)

    turns = queries.call_detail(a.call_id)["transcript"]
    caller, agent = turns[-2], turns[-1]
    assert caller["speaker"] == "carrier"
    assert caller["eou"] == 3.01 and caller["stt"] == 0.5 and caller["max"] is True
    assert agent["speaker"] == "agent"
    assert agent["latency_secs"] is not None
    assert agent["compose"] is not None
    assert agent["ttfb"] == 0.18 and agent["speech"] == 6.3
    assert "streamed" not in agent                     # the stub composed it whole

    row = queries.calls()[0]
    assert row["flags"].get("waited_max") == 1


def test_a_slow_reply_and_a_maximum_wait_are_derived_flags_and_filters(dash):
    repo, queries = dash
    a = _brain(repo)
    a.handle("about L1001", heard_timing={"eou": 1.0})
    # Make the reply look slow: the clock is ours to read, so write the number
    # the worker would have measured on a 4.6 s compose.
    a._turn_meta[-1]["latency"] = 4.6
    a._sync_transcript()
    b = _brain(repo)
    b.handle("about L1001", heard_timing={"eou": 2.0, "max": True})

    rows = {r["call_id"]: r for r in queries.calls()}
    assert rows[a.call_id]["flags"] == {"slow_turn": 1}
    assert rows[a.call_id]["slowest_turn_secs"] == 4.6
    assert rows[b.call_id]["flags"] == {"waited_max": 1}
    assert {r["call_id"] for r in queries.calls(flag="slow_turn")} == {a.call_id}
    assert {r["call_id"] for r in queries.calls(flag="waited_max")} == {b.call_id}
    assert SLOW_TURN_SECS == 4.0
    # A streamed reply is judged on when its first text arrived, not on the
    # whole compose the caller never waited for.
    b._turn_meta[-1].update({"latency": 5.2, "first_text": 1.9, "streamed": True})
    b._sync_transcript()
    rows = {r["call_id"]: r for r in queries.calls()}
    assert rows[b.call_id]["slowest_turn_secs"] == 1.9
    assert "slow_turn" not in rows[b.call_id]["flags"]
    assert b.call_id not in {r["call_id"] for r in queries.calls(flag="slow_turn")}


def test_the_timing_fields_pass_through_and_old_rows_still_render():
    turns = [["agent", "Hi."], ["carrier", "Load 1."], ["agent", "Got it."]]
    meta = json.dumps([{"t": 0.5, "latency": None},
                       {"t": 4.0, "latency": None, "eou": 1.01, "stt": 0.4},
                       {"t": 8.2, "latency": 3.4, "compose": 2.9, "first_text": 1.8,
                        "streamed": True, "ttfb": 0.2, "speech": 5.1}])
    out = _transcript_with_timing(turns, meta)
    assert out[1]["eou"] == 1.01 and out[1]["stt"] == 0.4 and "max" not in out[1]
    assert out[2]["first_text"] == 1.8 and out[2]["streamed"] is True
    assert out[2]["latency_secs"] == 3.4 and out[2]["ttfb"] == 0.2
    # A call from before the clock existed: every line, no timing.
    plain = _transcript_with_timing(turns, None)
    assert [t["text"] for t in plain] == ["Hi.", "Load 1.", "Got it."]
    assert all(t["elapsed_secs"] is None for t in plain)


def test_the_worker_reads_the_callers_wait_off_the_framework_message():
    settings = get_settings().model_copy(update={"max_endpointing_delay": 2.0,
                                                 "turn_detector_enabled": True})
    msg = SimpleNamespace(metrics={"end_of_turn_delay": 1.998, "transcription_delay": 0.44})
    assert worker._heard_timing(msg, settings) == {"eou": 2.0, "stt": 0.44, "max": True}
    quick = SimpleNamespace(metrics={"end_of_turn_delay": 1.0, "transcription_delay": 0.5})
    assert worker._heard_timing(quick, settings) == {"eou": 1.0, "stt": 0.5}
    assert worker._heard_timing(SimpleNamespace(metrics={}), settings) is None
    off = settings.model_copy(update={"turn_detector_enabled": False})
    assert "max" not in worker._heard_timing(msg, off)


def test_the_worker_keeps_the_voices_numbers_for_the_reply_in_flight(repo, monkeypatch):
    a = worker.CarrierAgent(repo, StubComposer(), tts=None)
    tts = SimpleNamespace(type="tts_metrics", ttfb=0.21, audio_duration=4.0, cancelled=False,
                          characters_count=80)
    a.on_metrics_collected(SimpleNamespace(metrics=tts))        # outside a turn: ignored
    assert a._turn_voice() == {}
    a._turn_in_flight = True
    a.on_metrics_collected(SimpleNamespace(metrics=tts))
    a.on_metrics_collected(SimpleNamespace(metrics=SimpleNamespace(
        type="tts_metrics", ttfb=0.9, audio_duration=2.5, cancelled=True, characters_count=40)))
    a.on_metrics_collected(SimpleNamespace(metrics=SimpleNamespace(
        type="eou_metrics", transcription_delay=0.4, end_of_utterance_delay=1.0)))
    assert a._turn_voice() == {"ttfb": 0.21, "speech": 6.5, "cut": True}


# --------------------------------------------------------------------------- #
# The worker's heartbeat
# --------------------------------------------------------------------------- #
def test_no_heartbeat_reads_as_no_worker(dash):
    _repo, queries = dash
    assert queries.worker_status() is None
    assert queries.overview()["worker"] is None


def test_a_fresh_heartbeat_reads_as_up_with_its_build_and_settings(dash):
    repo, queries = dash
    settings = get_settings()
    worker.write_heartbeat(2, settings, repo=repo)
    status = queries.worker_status()
    assert status["state"] == "up"
    assert status["calls_live"] == 2
    assert status["build"] == worker._BUILD and status["build"]
    assert status["settings"]["max_endpointing_delay"] == settings.max_endpointing_delay
    assert status["settings"]["interruption_mode"] == "vad"
    assert status["settings"]["stream_compose"] is settings.stream_compose
    assert status["age_secs"] < 5
    assert queries.overview()["worker"]["state"] == "up"


def test_an_old_heartbeat_reads_as_stale_then_down(dash):
    repo, queries = dash
    worker.write_heartbeat(0, get_settings(), repo=repo)
    conn = repo._db.connect()
    try:
        for age, expected in ((120, "stale"), (900, "down")):
            then = (datetime.datetime.now(datetime.UTC)
                    - datetime.timedelta(seconds=age)).isoformat()
            conn.execute("UPDATE worker_status SET last_seen=?", (then,))
            conn.commit()
            assert queries.worker_status()["state"] == expected
    finally:
        conn.close()


def test_the_heartbeat_never_raises(tmp_path):
    class _Broken:
        def record_worker_status(self, *_a, **_k):
            raise RuntimeError("locked")

    worker.write_heartbeat(0, get_settings(), repo=_Broken())   # logged, not raised


def test_the_settings_summary_carries_the_turn_taking_knobs_and_no_secrets():
    summary = worker.settings_summary(get_settings())
    for key in ("min_endpointing_delay", "max_endpointing_delay", "interruption_mode",
                "min_interruption_words", "stream_compose", "llm", "stt", "tts"):
        assert key in summary
    assert not any("key" in k or "secret" in k or "password" in k for k in summary)


def test_the_load_reporter_writes_a_heartbeat_at_most_every_interval(monkeypatch):
    written = []
    monkeypatch.setattr(worker, "write_heartbeat", lambda active, settings: written.append(active))
    monkeypatch.setattr(worker, "_last_heartbeat", 0.0)
    server = SimpleNamespace(active_jobs=[1, 2])
    worker._report_call_load(server)
    worker._report_call_load(server)                # within the interval: skipped
    assert written == [2]


# --------------------------------------------------------------------------- #
# The worker records what it sees on the line
# --------------------------------------------------------------------------- #
def test_a_dropped_backchannel_and_a_committed_late_answer_are_events(repo, monkeypatch):
    class _Session:
        user_state = "listening"
        current_speech = None

        def commit_user_turn(self, **_kw):
            fut = asyncio.get_running_loop().create_future()
            fut.set_result("")
            return fut

        def clear_user_turn(self):
            pass

    monkeypatch.setattr(worker.CarrierAgent, "session", property(lambda self: _Session()))
    a = worker.CarrierAgent(repo, StubComposer(), tts=None)
    a.brain.greet_with("Circle Logistics, this is Alex.")
    import time
    a._pending_final = ("9.", time.monotonic() - 20)
    asyncio.run(a._settle_withheld_transcript(more_coming=False))
    a._pending_final = ("Yes.", time.monotonic())
    asyncio.run(a._settle_withheld_transcript(more_coming=False))
    queries = DashboardQueries(repo._db)
    kinds = [e["kind"] for e in queries.call_detail(a.brain.call_id)["events"]]
    assert kinds == ["backchannel_dropped", "withheld_committed"]
