"""Dashboard: the read model over the audit trail, and the playground sessions.

The read model is exercised against calls REALLY driven through the agent, not
hand-inserted rows — the dashboard's contract is "shows what the agent wrote",
so the fixture is the agent writing. Sessions are pinned to the offline stub
composer (USE_LLM=false) the same way the rest of the suite is: a filled-in
`.env` on a developer machine must never make these tests reach a model.
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.dashboard.queries import DashboardQueries
from lanevoice.dashboard.sessions import SessionManager
from lanevoice.db import Database, Repository
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer

# The demo's "takes the opening offer" script: books L1001 at the board rate,
# with the booking link going to an address already on Blue Sky's file.
BOOKING_TURNS = (
    "about L1001", "MC 123456", "empty in Dallas, Texas today",
    "yeah that works", "yep, I can cover it",
    "billing at blue sky logistics dot com",
)


@pytest.fixture
def dash(tmp_path):
    db = Database(tmp_path / "dash.db")
    db.reset(seed=True)
    return Repository(db), DashboardQueries(db)


def _drive(repo, turns=BOOKING_TURNS):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    for turn in turns:
        agent.handle(turn)
        if agent.state.value == "done":
            break
    return agent


# --------------------------------------------------------------- read model #
def test_overview_counts_a_booked_call(dash):
    repo, queries = dash
    agent = _drive(repo)
    assert agent.outcome.value == "booked"

    ov = queries.overview()
    assert ov["kpis"]["total_calls"] == 1
    assert ov["kpis"]["booked"] == 1
    assert ov["kpis"]["booking_rate"] == 1.0
    assert ov["kpis"]["booked_value"] > 0
    booked_row = next(o for o in ov["outcomes"] if o["outcome"] == "booked")
    assert booked_row["count"] == 1
    # The call happened today, so the last day of the series carries it.
    assert ov["calls_by_day"][-1]["calls"] == 1
    assert ov["calls_by_day"][-1]["booked"] == 1
    assert len(ov["calls_by_day"]) == 30
    assert ov["recent"][0]["call_id"] == agent.call_id


def test_calls_list_row_shape(dash):
    repo, queries = dash
    agent = _drive(repo)

    rows = queries.calls()
    assert len(rows) == 1
    row = rows[0]
    assert row["call_id"] == agent.call_id
    assert row["outcome"] == "booked"
    assert row["lane"] == "Chicago, IL → Dallas, TX"
    assert row["carrier_name"] == "Blue Sky Logistics LLC"
    assert row["final_rate"] == 2000       # took the opening = the board rate
    assert row["turns"] and row["turns"] >= len(BOOKING_TURNS)
    assert row["duration_secs"] is not None
    # Driven straight through the agent, no session manager -> a phone call.
    assert row["source"] == "phone"


def test_the_carrier_name_survives_without_a_local_carriers_table(dash):
    """A live Transport Pro deployment keeps carriers in Transport Pro, not in
    this database's `carriers` table — that table only exists for the offline
    demo. Before the name was recorded on the call itself, the dashboard's join
    to `carriers` was the only source, so every real call showed a bare DOT
    number and never a name. Deleting the row here reproduces a live
    deployment's empty table; the call must still show the name."""
    repo, queries = dash
    agent = _drive(repo)

    conn = repo._db.connect()
    try:
        conn.execute("DELETE FROM carriers")
        conn.commit()
    finally:
        conn.close()

    row = queries.calls()[0]
    assert row["carrier_name"] == "Blue Sky Logistics LLC"
    assert row["carrier_mc"] == "MC123456"

    # And it is searchable by that name, not only by DOT number.
    assert [r["call_id"] for r in queries.calls(q="Blue Sky")] == [agent.call_id]


def test_calls_filters(dash):
    repo, queries = dash
    _drive(repo)
    # A second call that never finishes: constructed (start_call fires) and
    # dropped — exactly the trace a mid-flight hangup leaves.
    CarrierSalesAgent(repo, StubComposer(), settings=get_settings())

    assert len(queries.calls()) == 2
    assert len(queries.calls(outcome="booked")) == 1
    assert len(queries.calls(outcome="no_deal")) == 0
    assert len(queries.calls(outcome="incomplete")) == 1
    assert len(queries.calls(q="L1001")) == 1
    assert len(queries.calls(q="Blue Sky")) == 1
    assert len(queries.calls(q="no-such-thing")) == 0


def test_call_detail_carries_the_audit_trail(dash):
    repo, queries = dash
    agent = _drive(repo)

    detail = queries.call_detail(agent.call_id)
    assert detail is not None
    # Transcript is [speaker, line] pairs, both parties present.
    speakers = {who for who, _ in detail["transcript"]}
    assert speakers == {"agent", "carrier"}
    # Round 0 is the agent's opening at the board rate.
    opening = detail["offers"][0]
    assert (opening["round"], opening["party"], opening["amount"]) == (0, "agent", 2000.0)
    assert detail["load"]["load_id"] == "L1001"
    assert queries.call_detail("CALL-nope") is None


def test_transcript_is_live_during_the_call(dash):
    """The agent persists the transcript after every turn, so the dashboard can
    show a call while it is still on the line — and the final goodbye line
    (composed after `_finish` writes the record) is not lost."""
    repo, queries = dash
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    # Mid-call, no outcome yet — but the greeting is already readable.
    row = queries.calls()[0]
    assert row["outcome"] is None
    assert row["turns"] == 1

    agent.handle("about L1001")
    assert queries.calls()[0]["turns"] == 3   # greeting + caller + reply

    for text in BOOKING_TURNS[1:]:
        agent.handle(text)
    detail = queries.call_detail(agent.call_id)
    assert detail["outcome"] == "booked"
    # Every in-memory turn made it to disk, including the post-finish goodbye.
    assert len(detail["transcript"]) == len(agent.transcript)
    assert detail["transcript"][-1][0] == "agent"


def test_playback_cut_lands_in_the_timeline(dash):
    """When the caller barges in over a line, the record says so — otherwise the
    transcript shows a line the caller never heard, with nothing to explain it."""
    repo, queries = dash
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    line = agent.greeting()
    agent.note_playback_cut(line)
    notes = queries.call_detail(agent.call_id)["notes"]
    assert any("cut off mid-play" in n["note"] and line in n["note"] for n in notes)


def test_abandon_persists_the_transcript(dash):
    """A caller hangup mid-call must not lose what was said (worker calls
    `abandon()` on disconnect)."""
    repo, queries = dash
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    agent.handle("about L1001")
    agent.abandon()

    row = queries.calls(outcome="abandoned")[0]
    assert row["call_id"] == agent.call_id
    assert row["turns"] >= 3          # greeting + caller turn + reply, at least
    detail = queries.call_detail(agent.call_id)
    assert any(who == "carrier" for who, _ in detail["transcript"])

    # Idempotent, and never overwrites a real outcome.
    booked = _drive(repo)
    booked.abandon()
    assert queries.call_detail(booked.call_id)["outcome"] == "booked"


def test_unparseable_transcript_degrades_to_empty(dash):
    repo, queries = dash
    repo.start_call("CALL-raw")
    repo.end_call("CALL-raw", None, None, "abandoned", "not json at all")
    row = queries.calls(outcome="abandoned")[0]
    assert row["turns"] is None
    assert queries.call_detail("CALL-raw")["transcript"] == []


def test_reset_board_reopens_loads_but_keeps_calls(dash):
    repo, queries = dash
    agent = _drive(repo)
    assert repo.get_load("L1001").status.value == "covered"

    queries.reset_board()
    assert repo.get_load("L1001").status.value == "open"
    assert queries.call_detail(agent.call_id) is not None   # history survives


# ---------------------------------------------------------------- sessions #
@pytest.fixture
def offline_session_env(tmp_path, monkeypatch):
    """Sessions read the global settings, so pin the DB to the temp dir and the
    composer to the offline stub for the duration of the test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("USE_LLM", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_playground_session_runs_the_real_agent(offline_session_env):
    mgr = SessionManager()
    started = mgr.start(live=False)
    # `greeting()` speaks and moves the machine on to identifying the load.
    assert started["state"] == "identify_load"
    assert started["data_source"] == "sqlite"
    assert started["greeting"]
    assert started["composer"].startswith("offline stub")

    # The marker note makes the run show as a playground call, not a phone call.
    queries = DashboardQueries(Database(get_settings().db_path))
    assert queries.calls()[0]["source"] == "playground"

    res = mgr.turn(started["session_id"], "about L1001")
    assert res["state"] == "verify_carrier"
    assert res["done"] is False
    # The facts capture is the same data the demo's --facts flag prints.
    assert res["facts"] and all("directive" in t for t in res["facts"])

    for text in BOOKING_TURNS[1:]:
        res = mgr.turn(started["session_id"], text)
    assert res["done"] is True
    assert res["summary"]["outcome"] == "booked"
    # A finished session is forgotten; the audit trail is its record.
    with pytest.raises(KeyError):
        mgr.turn(started["session_id"], "hello?")


def test_ended_session_is_gone(offline_session_env):
    mgr = SessionManager()
    started = mgr.start(live=False)
    assert mgr.end(started["session_id"]) is True
    assert mgr.end(started["session_id"]) is False
    # Hanging up finalizes the record like a phone hangup: outcome + transcript.
    queries = DashboardQueries(Database(get_settings().db_path))
    detail = queries.call_detail(started["call_id"])
    assert detail["outcome"] == "abandoned"
    assert detail["transcript"]          # the greeting survived


# ------------------------------------------------------------------ server #
def test_http_routes_serve_json(offline_session_env):
    from lanevoice.dashboard.server import DashboardApp, serve

    httpd = serve("127.0.0.1", 0, DashboardApp())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as res:
                assert res.status == 200
                return json.loads(res.read())

        overview = get("/api/overview")
        assert set(overview) == {"kpis", "outcomes", "calls_by_day", "recent"}
        config = get("/api/config")
        assert config["data_source"] == "sqlite"
        # Key PRESENCE only — the config route must never carry a secret.
        assert "llm_key_present" in config["models"]
        flat = json.dumps(config).lower()
        assert "password" not in flat and "token" not in flat and "secret" not in flat
        loads = get("/api/loads")
        assert any(load["load_id"] == "L1001" for load in loads["loads"])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# -------------------------------------------------------------- recordings #
def test_call_detail_reports_and_serves_its_recording(tmp_path, offline_session_env):
    """RECORD_CALLS drops `call_recordings/<call_id>.ogg` next to the DB; the
    detail flag and the audio route are what let the Runs drawer play it. A
    call without a file must read as "no recording", never as an error — every
    call made before the feature existed is exactly that call."""
    from lanevoice.dashboard.server import DashboardApp, serve

    app = DashboardApp()
    repo = Repository(Database(get_settings().db_path))
    agent = _drive(repo)

    assert app.queries.call_detail(agent.call_id)["has_recording"] is False

    rec_dir = Path(get_settings().db_path).parent / "call_recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / f"{agent.call_id}.ogg").write_bytes(b"OggS-fake-audio")
    assert app.queries.call_detail(agent.call_id)["has_recording"] is True

    httpd = serve("127.0.0.1", 0, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/calls/{agent.call_id}/recording") as res:
            assert res.status == 200
            assert res.headers["Content-Type"] == "audio/ogg"
            assert res.read() == b"OggS-fake-audio"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/calls/CALL-nope/recording")
        assert exc.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_the_worker_copies_the_recording_out_of_the_doomed_temp_dir(tmp_path):
    """livekit-agents deletes the session temp dir at job cleanup — the copy is
    the difference between a replayable call and a file that never existed."""
    from lanevoice.telephony.worker import save_call_recording

    session_dir = tmp_path / "job-session"
    session_dir.mkdir()
    (session_dir / "audio.ogg").write_bytes(b"OggS-fake-audio")
    db_path = tmp_path / "audit" / "carrier_agent.db"

    saved = save_call_recording(session_dir, "CALL-abc123", db_path)
    assert saved == tmp_path / "audit" / "call_recordings" / "CALL-abc123.ogg"
    assert saved.read_bytes() == b"OggS-fake-audio"

    # No file (recording off, or the recorder never produced one): a quiet None.
    assert save_call_recording(tmp_path / "empty", "CALL-x", db_path) is None
