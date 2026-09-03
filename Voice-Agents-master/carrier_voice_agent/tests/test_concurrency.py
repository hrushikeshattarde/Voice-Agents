"""Two callers at once: a cap the worker reports to LiveKit, and a recording save
that does not fail in silence."""

import logging
from pathlib import Path

from lanevoice.telephony import worker


def test_load_is_calls_over_the_cap_and_full_at_the_cap():
    assert worker.call_load(0, 4) == 0.0
    assert worker.call_load(2, 4) == 0.5
    assert worker.call_load(4, 4) == 1.0
    assert worker.call_load(9, 4) == 1.0                    # never above 1
    assert worker.call_load(3, 0) == 0.0                    # cap off: nothing to report
    threshold = worker.full_threshold(4)
    assert worker.call_load(3, 4) < threshold <= worker.call_load(4, 4)
    assert worker.full_threshold(1) == 0.5


def test_a_recording_under_another_name_is_still_saved(tmp_path: Path):
    session = tmp_path / "session"
    session.mkdir()
    (session / "session_xyz.ogg").write_bytes(b"OggS-not-really")
    saved = worker.save_call_recording(session, "CALL-t1", tmp_path / "db.sqlite")
    assert saved == tmp_path / "call_recordings" / "CALL-t1.ogg"
    assert saved.read_bytes() == b"OggS-not-really"


def test_a_missing_recording_is_logged_with_the_directory(tmp_path: Path, caplog):
    session = tmp_path / "session"
    session.mkdir()
    (session / "session_report.json").write_text("{}")
    with caplog.at_level(logging.WARNING, logger="lanevoice.worker"):
        assert worker.save_call_recording(session, "CALL-t2", tmp_path / "db.sqlite") is None
    assert "no recording file for call CALL-t2" in caplog.text
    assert "session_report.json" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lanevoice.worker"):
        assert worker.save_call_recording(session, "CALL-t3", tmp_path / "db.sqlite",
                                          warn=False) is None
    assert "no recording file" not in caplog.text
