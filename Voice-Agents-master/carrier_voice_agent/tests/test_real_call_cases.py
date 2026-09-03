"""What a real call does that a test script does not: the caller goes quiet,
cannot be heard, asks about an invoice, names a state instead of a city — and
the trunk cannot yet move a call, so "transferring you" must not be said.
"""

from lanevoice import geo
from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer


def _notes(repo):
    conn = repo._db.connect()
    try:
        return [r[0] for r in conn.execute("SELECT note FROM call_notes ORDER BY id").fetchall()]
    finally:
        conn.close()


def _agent(repo, **overrides):
    settings = get_settings().model_copy(update=overrides) if overrides else get_settings()
    a = CarrierSalesAgent(repo, StubComposer(), settings=settings)
    a.greeting()
    return a


def _directive(a) -> str:
    return a._composer.turns[-1]["directive"]


def test_requirements_follow_the_load_without_waiting_for_a_sure(repo):
    a = _agent(repo)
    a.handle("load L1002")                       # seeded with requirements
    a.handle("MC 123456")
    a.handle("empty in Chicago Illinois tomorrow morning")
    assert a.pending_followup is True
    assert "requirements" in _directive(a).lower()          # the pitch says they are coming
    more = a.continue_turn()
    assert more and a._requirements_read is True and a.pending_followup is False
    assert "cover this load's requirements" in _directive(a)
    assert a.continue_turn() is None


def test_a_state_alone_is_not_a_place(repo):
    assert geo.state_only("Indiana") == "IN"
    assert geo.state_only("IN") == "IN"
    assert geo.state_only("Fort Wayne, Indiana") is None
    a = _agent(repo)
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("Indiana at 10 am")
    assert a._empty_location is None and a._empty_when and "10" in a._empty_when
    assert "which city in IN" in _directive(a)
    a.handle("Fort Wayne")
    assert a._empty_location and "Fort Wayne" in a._empty_location
    assert a.state.value == "check_requirements" or a.state.value == "state_price"


def test_an_invoice_question_goes_to_a_person_not_a_load_hunt(repo):
    a = _agent(repo)
    a.handle("I'm calling about an invoice from last week that hasn't been paid")
    assert a.summary()["outcome"] == "transferred"
    assert any("not carrier sales" in n for n in _notes(repo))


def test_giving_up_on_an_unheard_caller_leaves_a_callback(repo):
    a = _agent(repo)
    a.set_caller("+12602649808")
    a.give_up_unheard("I'm having a hard time hearing you.")
    assert a.summary()["outcome"] == "transferred"
    assert any("CALLBACK NEEDED at +12602649808" in n for n in _notes(repo))
    assert a.transcript[-1] == ("agent", "I'm having a hard time hearing you.")


def test_a_silent_caller_is_closed_as_abandoned(repo):
    a = _agent(repo)
    a.handle("L1001")
    a.close_idle("Alright, I'll let you go.")
    assert a.summary()["outcome"] == "abandoned"
    assert any("went quiet" in n for n in _notes(repo))


def test_a_handoff_the_trunk_cannot_perform_promises_a_callback(repo):
    # The seeded reps have numbers, so the rep resolves — but SIP transfer is off
    # by default, and "transferring you" would be a lie followed by silence.
    a = _agent(repo)
    a.handle("L1001")
    a._transfer_and_say(reason="ceiling_guard")
    directive = _directive(a)
    assert "call them straight back" in directive
    assert "transferring them" not in directive
    assert a.pending_transfer is not None            # the worker still logs who it would dial

    b = _agent(repo, sip_transfer_enabled=True)
    b.handle("L1001")
    b._transfer_and_say(reason="ceiling_guard")
    assert "transferring them to" in _directive(b)


def test_a_placeable_town_is_recorded_under_the_tables_name(repo):
    a = _agent(repo)
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("empty in ft wayne indiana tomorrow morning")
    assert a._empty_place == "Fort Wayne, IN"
    assert "Fort Wayne, IN" in a._empty_summary()


def test_an_unplaceable_town_with_no_state_gets_one_question(repo):
    a = _agent(repo)
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("empty in Klondike Corner tomorrow")
    assert "which state" in _directive(a).lower()
    a.handle("Ohio")
    assert a._empty_location and "Ohio" in a._empty_location
