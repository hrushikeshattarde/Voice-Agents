"""Every call that reached a load leaves one summary note a rep can read on the
load: who called, how far it got, the money, and a one-line reason under a
label they can scan for. Written at call end for every outcome — a caller who
verified and hung up used to leave nothing on the load, and that is precisely
the caller the rep wants to ring back. The full turn-by-turn conversation used
to be pasted into this same note; it is still on the call record (the
dashboard's Transcript tab), just not duplicated here — a label plus one line
of why is what a rep scanning a list of calls actually needs.
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
    # Nothing decision-worthy happened before the hang-up, so there is no
    # specific reason to scan for and no note to fall back on — the summary
    # says plainly that this is a plain mid-call hang-up, not "Other" silently.
    assert "Label: Other" in summary
    assert "Summary: Caller hung up during the empty-truck question" in summary
    assert "Conversation:" not in summary

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


# --------------------------------------------------------------------------- #
# The label: one word from a fixed vocabulary a rep can scan a list of calls
# for, next to the one-line reason that already existed. Each case below drives
# a REAL call through the agent to the outcome in question, rather than setting
# `_end_reason` by hand — the label has to be right for the code path that
# actually produces it, not just for the mapping table in isolation.
# --------------------------------------------------------------------------- #
EMPTY = "empty in Dallas, Texas today"
ON_FILE = "dispatch@blueskylogistics.com"


def _label(repo, **overrides):
    agent = CarrierSalesAgent(repo, StubComposer(),
                              settings=get_settings().model_copy(update=overrides) if overrides
                              else get_settings())
    return agent


def test_a_booked_call_is_labelled_success(repo):
    a = _label(repo)
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 123456")                        # Blue Sky Logistics — active
    a.handle(EMPTY)
    a.handle("yeah that works")
    a.handle("yep, I can cover it")
    a.handle(ON_FILE)
    a.abandon()                                  # the worker always calls this at hang-up
    assert a.summary()["outcome"] == "booked"

    summary = _summaries(repo)[0]
    assert "Label: Success" in summary


def test_a_rate_thats_too_high_is_labelled_as_such(repo):
    a = _label(repo, max_negotiation_rounds=4)
    a.greeting()
    a.handle("load L1003")
    a.handle("MC654321")
    a.handle(EMPTY)
    for _ in range(5):
        a.handle("I need 1500")
        if a.state.value == "done":
            break
    a.abandon()
    assert a.summary()["outcome"] == "no_deal"

    summary = _summaries(repo)[0]
    assert "Label: Rate too high" in summary
    assert "Summary: Carrier's number stayed above what we could pay." in summary


def test_an_inactive_carrier_is_labelled_not_qualified(repo):
    a = _label(repo)
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 555444")                        # Dormant Transport — inactive
    a.handle("yeah, that's us")                  # the name read back is confirmed first
    a.abandon()
    assert a.summary()["outcome"] == "rejected"

    summary = _summaries(repo)[0]
    assert "Label: Carrier not qualified" in summary
    assert "Summary: Carrier's authority is not active." in summary


def test_a_carrier_who_asks_for_a_person_is_labelled_that_way(repo):
    a = _label(repo)
    a.greeting()
    a.handle("can you transfer me to a rep")
    a.abandon()
    assert a.summary()["outcome"] == "transferred"

    summary = _summaries(repo)[0]
    assert "Label: Ask for transfer to human" in summary


def test_an_unsellable_load_falls_to_other_with_its_own_reason(repo):
    """Covered, out of scope, no more numbers to try, a booking failure — none
    of these is the caller's fault or a rate dispute, and none of the seven
    named labels fits. "Other" is exactly what it is for; the summary line
    still says precisely what happened."""
    a = _label(repo)
    a.greeting()
    a.handle("L1004")                             # seeded as covered
    a.abandon()
    assert a.summary()["outcome"] == "no_deal"

    summary = _summaries(repo)[0]
    assert "Label: Other" in summary
    assert "Summary: Load is already covered by another carrier." in summary
