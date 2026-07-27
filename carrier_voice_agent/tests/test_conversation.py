"""End-to-end flow tests through the CarrierSalesAgent (no models, no keys)."""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings


def _agent(repo, max_rounds=6):
    settings = get_settings().model_copy(update={"max_negotiation_rounds": max_rounds})
    return CarrierSalesAgent(repo, phraser=None, settings=settings)


def test_happy_path_books_at_opening(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 123456")
    a.handle("yeah that works")        # agree on rate -> confirm step
    assert a.state.value == "confirm_booking"
    a.handle("yep, I can cover it")    # confirm pickup -> collect details
    assert a.state.value == "collect_details"
    a.handle("dispatch@blue.com, driver Mike 555-123-4567")   # -> finalize
    assert a.summary()["outcome"] == "booked"
    assert repo.get_load("L1001").status.value == "covered"


def test_walk_up_then_book(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("I need 2080")     # high ask -> hold firm
    a.handle("2050")            # we split the difference and counter
    a.handle("deal")            # accept the offer on the table -> confirm step
    a.handle("yep can cover it")  # confirm pickup -> collect details
    a.handle("bill@carrier.com, driver Sam 555-999-0000")  # -> finalize
    assert a.summary()["outcome"] == "booked"


def test_cannot_cover_pickup_is_transferred(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 123456")
    a.handle("yeah that works")        # agree -> confirm step
    a.handle("actually I can't make that pickup day")
    assert a.summary()["outcome"] == "transferred"


def test_high_ask_ends_no_deal_with_note(repo):
    a = _agent(repo, max_rounds=4)
    a.greeting()
    a.handle("load L1003")
    a.handle("MC654321")
    for _ in range(5):
        a.handle("I need 1500")
        if a.state.value == "done":
            break
    assert a.summary()["outcome"] == "no_deal"
    conn = repo._db.connect()
    notes = conn.execute("SELECT note FROM call_notes").fetchall()
    conn.close()
    assert any("NO DEAL" in n["note"] for n in notes)


def test_revoked_authority_is_rejected(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1002")
    a.handle("MC999888")
    assert a.summary()["outcome"] == "rejected"


def test_fraud_low_is_transferred(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC123456")
    a.handle("I'll haul it for 900")
    assert a.summary()["outcome"] == "transferred"


def test_unposted_load_is_not_offered(repo):
    a = _agent(repo)
    a.greeting()
    reply = a.handle("L1005")               # seeded as is_posted=0
    assert a.state.value == "identify_load"  # did NOT proceed to verification
    assert "posted" in reply.lower()


def test_not_approved_carrier_declined(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 222333")                    # verified but not approved
    assert a.summary()["outcome"] == "rejected"


def test_load_requirements_accepted_leads_to_price(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1002")                        # L1002 has special requirements
    a.handle("MC 123456")
    assert a.state.value == "check_requirements"
    a.handle("yeah, I can do that")
    assert a.state.value == "state_price"


def test_load_requirements_declined_no_deal(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1002")
    a.handle("MC 123456")
    a.handle("no, I can't run it that cold")
    assert a.summary()["outcome"] == "no_deal"


def test_carrier_asks_for_human(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC123456")
    a.handle("can I talk to a rep")
    assert a.summary()["outcome"] == "transferred"
