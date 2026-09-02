"""What a caller waits on inside one turn, and what no longer makes them wait.

Three things moved off the critical path or onto the log in one change:

* The carrier enrichment lookups (Highway, HappyRobot) run WHILE Transport Pro is
  answering, keyed on the number as heard. When the record confirms that number
  is the MC, the results are reused; when the caller gave a DOT they are
  discarded and the lookups run again on the right identifiers.
* Notes mirrored onto the load in the TMS are posted from a background thread;
  `flush_notes` collects them at call end.
* Every turn records where its time went, and the session's turn-taking rules
  are built from settings in one place.
"""

from __future__ import annotations

import json
import threading

import httpx

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer
from tests.transportpro_fake import HAPPYROBOT_URL, FakeTransportPro, repository
from tests.transportpro_payloads import CARRIER_STATUS_ACTIVE, EMPTY_SEARCH

_HR = {"happyrobot_url": HAPPYROBOT_URL, "happyrobot_token": "hr-token"}

# The HappyRobot row for the seed carrier, carrying the classification list that
# `/voiceai/carrier_status` does not.
HR_ROW = {
    "action": "carrier_lookup",
    "data": [{
        "id": 13167,
        "status": "ACTIVE",
        "carrier_name": "Blue Sky Logistics LLC",
        "mc_number": 123456,
        "us_dot_number": 1000001,
        "classifications": ["Critical Cargo"],
    }],
}


def _hr_lookups(fake) -> list[dict]:
    """The `data` of every HappyRobot carrier_lookup the repository sent, in order."""
    out = []
    for request in fake.calls("happyrobot.php"):
        body = json.loads(request.content)
        if body["action"] == "carrier_lookup":
            out.append(body["data"])
    return out


def test_enrichment_started_on_the_heard_mc_is_reused_not_repeated(repo):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.json("/svc/happyrobot.php", HR_ROW)

    carrier = repository(fake, repo, **_HR).get_carrier("123456")

    assert carrier is not None
    assert "Critical Cargo" in carrier.qualifications          # the enrichment landed
    # One HappyRobot read, keyed on the MC the caller gave — the speculative lookup
    # WAS the enrichment lookup, so nothing ran twice on the critical path.
    assert _hr_lookups(fake) == [{"mc_number": "123456"}]


def test_a_dot_given_by_the_caller_redoes_enrichment_on_the_records_mc(repo):
    fake = FakeTransportPro()

    def carrier_status(request):
        if "dot_number" in request.url.params:
            return httpx.Response(200, json=CARRIER_STATUS_ACTIVE)
        return httpx.Response(200, json=EMPTY_SEARCH)   # no MC match

    fake.on("/voiceai/carrier_status", carrier_status)
    fake.json("/svc/happyrobot.php", HR_ROW)

    carrier = repository(fake, repo, **_HR).get_carrier("1000001")   # their DOT

    assert carrier is not None
    assert "Critical Cargo" in carrier.qualifications
    lookups = _hr_lookups(fake)
    # The speculative read was keyed on the DOT digits as if they were an MC. The
    # record said otherwise, so that result was thrown away and the enrichment
    # ran on the MC the record actually carries — never on the wrong identifier.
    # (Two reads, unordered: the speculative one ran on another thread.)
    assert sorted(d["mc_number"] for d in lookups) == ["1000001", "123456"]


class _RecordingRepo:
    """The seeded SQLite repository, with `post_load_note` made observable and
    slow: it blocks until the test lets it through, which is how the test can
    tell whether the agent waited for it."""

    def __init__(self, inner):
        self._inner = inner
        self.posted: list[tuple[str, str]] = []
        self.release = threading.Event()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def post_load_note(self, load_id: str, content: str) -> bool:
        self.release.wait(timeout=5)
        self.posted.append((load_id, content))
        return True


def _agent_with_load(recording) -> CarrierSalesAgent:
    agent = CarrierSalesAgent(recording, StubComposer(), settings=get_settings())
    agent.greeting()
    agent.handle("calling about L1001")          # sets the load the notes attach to
    return agent


def test_tms_notes_no_longer_hold_up_the_turn(repo):
    recording = _RecordingRepo(repo)
    agent = _agent_with_load(recording)
    agent._note("the caller said something worth a rep's attention")
    # `_note` returned while the TMS write was still blocked — the caller was not
    # waiting on Transport Pro. The local audit trail was written synchronously.
    assert recording.posted == []
    conn = repo._db.connect()
    try:
        local = [r[0] for r in conn.execute("SELECT note FROM call_notes").fetchall()]
    finally:
        conn.close()
    assert any("worth a rep's attention" in note for note in local)

    recording.release.set()
    agent.flush_notes()
    assert len(recording.posted) == 1
    load_id, content = recording.posted[0]
    assert load_id == "L1001"
    assert content.startswith(f"[Voice AI call {agent.call_id}]")


def test_abandon_collects_the_notes_still_in_flight(repo):
    recording = _RecordingRepo(repo)
    agent = _agent_with_load(recording)
    agent._note("first")
    agent._note("second")
    recording.release.set()
    agent.abandon()
    assert [content.split("] ", 1)[1] for _, content in recording.posted] == ["first", "second"]


def test_every_turn_records_where_its_time_went(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    agent.handle("calling about L1001")
    timing = agent.last_turn_timing
    assert timing["compose_calls"] == 1
    assert timing["total"] >= timing["compose"] >= 0
    assert abs(timing["total"] - timing["compose"] - timing["other"]) < 1e-6
    assert timing["state"] == agent.state.value


def test_turn_handling_is_built_from_settings():
    from lanevoice.telephony.worker import turn_handling

    settings = get_settings().model_copy(update={
        "min_endpointing_delay": 0.6, "max_endpointing_delay": 2.5,
        "allow_interruptions": True, "min_interruption_duration": 0.8,
        "min_interruption_words": 3, "resume_false_interruption": True,
        "false_interruption_timeout": 1.5,
    })
    options = turn_handling(settings)
    assert options["endpointing"] == {"min_delay": 0.6, "max_delay": 2.5}
    assert options["interruption"] == {
        "enabled": True, "min_duration": 0.8, "min_words": 3,
        "resume_false_interruption": True, "false_interruption_timeout": 1.5,
    }
