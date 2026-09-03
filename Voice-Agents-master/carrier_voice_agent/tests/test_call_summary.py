"""Every call that reached a load leaves one summary note a rep can read on the
load: who called, how far it got, the money, the conversation. Written at call
end for every outcome — a caller who verified and hung up used to leave nothing
on the load, and that is precisely the caller the rep wants to ring back.
"""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer


def _notes(repo):
    conn = repo._db.connect()
    try:
        return [r[0] for r in conn.execute("SELECT note FROM call_notes ORDER BY id").fetchall()]
    finally:
        conn.close()


def _summaries(repo):
    return [n for n in _notes(repo) if "CALL SUMMARY" in n]


def test_a_hangup_after_verification_still_says_who_called(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.set_caller("+12602649808")
    agent.greeting()
    agent.handle("load L1001")
    agent.handle("MC 123456")                    # Blue Sky Logistics — active
    agent.abandon()                              # hung up at the empty-truck question

    (summary,) = _summaries(repo)
    assert "Caller: +12602649808" in summary
    assert "Blue Sky Logistics LLC (MC 123456" in summary
    assert "Load: L1001" in summary
    assert "Got as far as: the empty-truck question" in summary
    assert "Outcome: caller hung up" in summary
    assert "Callback: +12602649808 — Blue Sky Logistics LLC" in summary
    assert "Conversation:" in summary and "  Caller: MC 123456" in summary

    conn = repo._db.connect()
    try:
        row = conn.execute("SELECT caller_number, outcome FROM calls WHERE call_id=?",
                           (agent.call_id,)).fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("+12602649808", "abandoned")


def test_a_caller_who_never_gave_an_mc_is_still_on_the_load(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.set_caller("+19303334183")
    agent.greeting()
    agent.handle("calling about L1001")
    agent.abandon()

    (summary,) = _summaries(repo)
    assert "Carrier: not identified" in summary
    assert "Load: L1001" in summary
    assert "Got as far as: the MC/USDOT check" in summary
    assert "Callback: +19303334183" in summary


def test_the_summary_is_written_once_even_if_abandon_runs_twice(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    agent.handle("L1001")
    agent.abandon()
    agent.abandon()
    assert len(_summaries(repo)) == 1
    assert "Caller: number not available" in _summaries(repo)[0]


def test_a_declined_load_still_gets_the_summary_and_the_reason(repo):
    """A covered load is not sold, but its rep wants to know who called. Both the
    reason note and the summary go to the load the caller asked about."""
    posted = []
    original = repo.post_load_note
    repo.post_load_note = lambda load_id, content: posted.append((load_id, content)) or True
    try:
        agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
        agent.set_caller("+12602649808")
        agent.greeting()
        agent.handle("load L1004")                   # seeded as covered
        agent.abandon()
    finally:
        repo.post_load_note = original
    targets = {load_id for load_id, _ in posted}
    assert targets == {"L1004"}
    assert any("CALL SUMMARY" in content for _, content in posted)
    assert any("covered" in content for _, content in posted)


def test_rehearsal_loads_are_parsed_as_digits():
    s = get_settings().model_copy(update={"transport_pro_test_load_ids": " 2532717, L2554792 "})
    assert s.test_load_ids == frozenset({"2532717", "2554792"})
