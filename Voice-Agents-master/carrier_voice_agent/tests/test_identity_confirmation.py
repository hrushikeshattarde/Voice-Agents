"""A negative verdict waits for the caller to confirm the company it is about.

Observed live: a caller's MC 299953 reached the agent as "93" and then "99953".
The fragments resolved to a different carrier, one Highway fails, and the caller
was told their company didn't meet the requirements — a false accusation built
on two misheard digits. Now the company name is read back before any verdict
other than PROCEED is acted on: an affirmation lets the verdict stand, a denial
throws the digits away and asks for the number again.
"""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer


def _agent(repo) -> CarrierSalesAgent:
    a = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    a.greeting()
    a.handle("about L1001")
    return a


def _last_directive(a) -> str:
    return a._composer.turns[-1]["directive"].lower()


def test_a_decline_is_preceded_by_a_name_check(repo):
    a = _agent(repo)
    a.handle("MC 555444")                       # Dormant Transport — inactive
    assert a.summary()["outcome"] is None       # nothing decided yet
    assert a.state.value == "verify_carrier"
    directive = _last_directive(a)
    assert "company name" in directive and "right company" in directive
    assert "requirement" not in directive.split("say nothing")[0]
    assert "Dormant Transport" in a._composer.turns[-1]["facts"]


def test_an_affirmed_name_lets_the_decline_stand(repo):
    a = _agent(repo)
    a.handle("MC 555444")
    a.handle("yeah, that's us")
    assert a.summary()["outcome"] == "rejected"
    assert "does not currently meet the requirements" in _last_directive(a)


def test_a_denied_name_throws_the_digits_away_and_asks_again(repo):
    a = _agent(repo)
    a.handle("MC 555444")
    a.handle("no, that's not us")
    assert a.summary()["outcome"] is None
    assert a.state.value == "verify_carrier"
    assert a.carrier is None
    assert a._mc_digits == ""
    directive = _last_directive(a)
    assert "misheard" in directive and "mc or usdot" in directive

    a.handle("MC 123456")                       # the number they actually have
    assert a.state.value == "ask_empty"
    assert a.carrier.legal_name == "Blue Sky Logistics LLC"
    assert a.summary()["outcome"] is None


def test_a_handoff_verdict_also_waits_for_the_name_check(repo):
    a = _agent(repo)
    a.handle("MC 777111")                       # recently reactivated -> a rep
    assert a.summary()["outcome"] is None
    assert "company name" in _last_directive(a)
    a.handle("yes")
    assert a.summary()["outcome"] == "transferred"


def test_a_clean_carrier_is_not_made_to_confirm_twice(repo):
    a = _agent(repo)
    a.handle("MC 123456")                       # active, approved, insured
    assert a.state.value == "ask_empty"         # straight on, as before
