"""End-to-end flow tests through the CarrierSalesAgent (no models, no keys)."""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings


def _agent(repo, max_rounds=6, phraser=None):
    settings = get_settings().model_copy(update={"max_negotiation_rounds": max_rounds})
    return CarrierSalesAgent(repo, phraser=phraser, settings=settings)


class _LeakyPhraser:
    """Stands in for a phrasing LLM that invents a rate nobody authorized."""

    def phrase(self, instruction: str, context: str = "") -> str:
        return "Tell you what, meet me at $2200 and it's yours."


class _BareNumberPhraser:
    """Same leak, minus the dollar sign — spoken aloud it's still a new offer."""

    def phrase(self, instruction: str, context: str = "") -> str:
        return "We're still at $2000 on L1001 — can you meet me at 2200?"


class _DropsOurNumberPhraser:
    """Speaks no unauthorized figure, but quietly drops OUR offer and implies we
    took the carrier's number."""

    def phrase(self, instruction: str, context: str = "") -> str:
        return "You came down to $2400, I'll meet you there. We good?"


class _FaithfulPhraser:
    """Rewords the hold using only sanctioned numbers plus load/lane facts."""

    def phrase(self, instruction: str, context: str = "") -> str:
        return "$2500's rich for L1001 — I'm at $2000. How close can you get?"


def test_carrier_cannot_grind_the_agent_up_to_its_cap(repo):
    """L1001 floor $2000, Max Buy $2500. Even a carrier who concedes every turn
    only walks the agent up in shrinking steps — nowhere near the cap."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("I want 2500")
    for ask in (2450, 2400, 2350, 2300):
        a.handle(str(ask))
    conn = repo._db.connect()
    rows = conn.execute(
        "SELECT amount FROM negotiation_offers WHERE offered_by='agent'"
    ).fetchall()
    conn.close()
    assert max(int(r["amount"]) for r in rows) < 2200   # well short of the $2500 cap


def test_phraser_cannot_invent_a_rate(repo):
    """The engine owns every number: an unauthorized dollar figure from the LLM
    is discarded in favour of the deterministic template."""
    a = _agent(repo, phraser=_LeakyPhraser())
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    reply = a.handle("I want 2500")      # engine decides: hold at $2000
    assert "2200" not in reply           # the invented rate never reaches the caller
    assert "$2000" in reply              # we speak the safe template instead


def test_phraser_cannot_invent_a_rate_without_a_dollar_sign(repo):
    """A bare '2200' is a rate leak too — the caller hears a number the engine
    never sanctioned, and it moves the negotiation."""
    a = _agent(repo, phraser=_BareNumberPhraser())
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    reply = a.handle("I want 2500")
    assert "2200" not in reply
    assert "how close you can get to $2000" in reply   # deterministic hold template


def test_phraser_cannot_drop_our_offer(repo):
    """A reply that never states our counter reads as acceptance of theirs — the
    turn has to put OUR number on the table or the template does it for us."""
    a = _agent(repo, phraser=_DropsOurNumberPhraser())
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("I want 2500")              # hold
    reply = a.handle("2400")             # they moved $100 -> we PULL, still at $2000
    assert "$2000" in reply              # our number is spoken, not implied away
    assert "meet you there" not in reply  # the LLM's "I'll take yours" was discarded
    assert a.state.value == "negotiate"  # we did NOT slide into booking at $2400


def test_phrasing_survives_load_ids_and_dates(repo):
    """The leak guard must not throw away good phrasing just because it mentions
    the load ID — only unsanctioned MONEY is blocked."""
    a = _agent(repo, phraser=_FaithfulPhraser())
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    reply = a.handle("I want 2500")
    assert reply == "$2500's rich for L1001 — I'm at $2000. How close can you get?"


def test_happy_path_books_at_opening(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("about L1001")
    a.handle("MC 123456")
    a.handle("yeah that works")        # agree on rate -> confirm step
    assert a.state.value == "confirm_booking"
    a.handle("yep, I can cover it")    # confirm pickup -> confirm the email
    assert a.state.value == "confirm_email"
    a.handle("send it to dispatch@blue.com")   # -> finalize
    assert a.summary()["outcome"] == "booked"
    assert repo.get_load("L1001").status.value == "covered"


def test_walk_up_then_book(repo):
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("I need 2080")       # high ask -> hold firm, ask them to come down
    a.handle("2050")              # $50 apart: not worth haggling -> book it
    assert a.state.value == "confirm_booking"
    a.handle("yep can cover it")  # confirm pickup -> confirm the email
    a.handle("bill@carrier.com")  # -> finalize
    assert a.summary()["outcome"] == "booked"
    assert a._agreed_rate == 2050


def test_carrier_who_grinds_still_gets_covered(repo):
    """The reported call end-to-end: 2500 -> 2200 must book, not no-deal."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    for ask in ("I need 2500", "2500", "2400", "2300", "2200", "2200"):
        a.handle(ask)
        if a.state.value != "negotiate":
            break
    assert a.state.value == "confirm_booking"
    assert a._agreed_rate == 2200
    a.handle("yes I can cover it")
    a.handle("ops@blue.com, driver Dave 555-111-2222")
    assert a.summary()["outcome"] == "booked"


def test_firm_carrier_inside_max_buy_is_handed_to_a_rep(repo):
    """$2400 is inside Max Buy but above the agent's own authority — a human
    finishes it. The agent must not walk away from it."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    for _ in range(6):
        a.handle("2400, that's my number")
        if a.state.value == "done":
            break
    assert a.summary()["outcome"] == "transferred"
    conn = repo._db.connect()
    notes = conn.execute("SELECT note FROM call_notes").fetchall()
    conn.close()
    assert any("ESCALATION" in n["note"] for n in notes)


def test_holds_its_number_until_the_carrier_moves(repo):
    """The agent asks the carrier to come down instead of walking its own price
    up — repeating the same demand (or just saying no) buys nothing."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    first = a.handle("I want 2500")      # opening ask well above our floor
    assert "$2000" in first              # restates OUR number...
    assert "2500" in first               # ...acknowledges theirs, offers nothing new
    second = a.handle("no, 2500")        # still no movement from them
    assert "$2000" in second             # we're still at our floor
    conn = repo._db.connect()
    agent_offers = conn.execute(
        "SELECT amount FROM negotiation_offers WHERE offered_by='agent'"
    ).fetchall()
    conn.close()
    assert {int(r["amount"]) for r in agent_offers} == {2000}   # never raised itself


def test_agent_makes_the_carrier_come_down_instead_of_laddering(repo):
    """The rep behaviour: a carrier who keeps moving gets asked to keep moving.
    Across a whole grind the agent puts at most TWO numbers on the table — its
    opening and the single move it makes to close — never a $50-at-a-time
    walk-up that trades its own margin for their nickels."""
    a = _agent(repo, max_rounds=8)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    replies = [a.handle(t) for t in ("I need 2500", "2450", "2400", "2350")]
    # Each of their moves is answered with a question, not a concession.
    assert sum("how close" in r.lower() for r in replies) >= 1
    assert any("best you can" in r.lower() or "need to be" in r.lower() for r in replies)
    conn = repo._db.connect()
    offers = {int(r["amount"]) for r in conn.execute(
        "SELECT amount FROM negotiation_offers WHERE offered_by='agent'").fetchall()}
    conn.close()
    assert len(offers) <= 2          # the opening, plus one closing move
    assert min(offers) == 2000       # and it never abandoned its anchor


def test_a_declared_best_number_is_closed_not_pushed_again(repo):
    """'That's my best' ends the asking. Running the same 'how close can you
    get?' play at a carrier who already answered it is what makes a bot obvious —
    the agent makes its move instead."""
    a = _agent(repo)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    a.handle("I need 2400")                        # hold + discovery question
    reply = a.handle("2300, that's my best")       # -> our closing offer
    assert "how close" not in reply.lower()
    assert "$2150" in reply                        # 2000 + half the remaining $300
    assert a.neg.pulls == 0                        # never asked them to walk again


def test_the_same_pitch_is_never_repeated_at_the_caller(repo):
    """Non-price levers are spent one per turn. Hearing the identical selling
    line two or three times in one call is the most bot-like thing there is."""
    a = _agent(repo, max_rounds=8)
    a.greeting()
    a.handle("L1001")
    a.handle("MC 123456")
    replies = [a.handle(t) for t in ("I need 2500", "2500", "2400", "2300", "2200")]
    for lever in ("first in line", "sit on their dock", "same day"):
        assert sum(lever in r for r in replies) <= 1, f"repeated pitch: {lever}"


def _to_email_step(repo, load="about L1001", mc="MC 123456"):
    """Drive a call to the point where the agent asks where the rate con goes."""
    a = _agent(repo)
    a.greeting()
    a.handle(load)
    a.handle(mc)
    a.handle("yeah that works")          # take the opening rate
    ask = a.handle("yep, I can cover it")  # confirm pickup -> email question
    return a, ask


def test_agent_asks_for_the_email_and_suggests_nothing(repo):
    """The caller supplies the address. The agent must not read one off the file
    and put words in their mouth — and must not ask for driver/truck details."""
    a, ask = _to_email_step(repo)
    assert a.state.value == "confirm_email"
    assert "email" in ask.lower()
    assert "@" not in ask                                  # suggests no address
    assert "driver" not in ask.lower() and "truck" not in ask.lower()


def test_email_the_carrier_gives_is_matched_against_their_file(repo):
    """Blue Sky recites an address we already know: matched, nothing appended."""
    before = repo.carrier_emails("DOT1000001")
    assert len(before) > 1                                 # carriers have several
    a, _ = _to_email_step(repo)
    done = a.handle("send it to billing at blue sky logistics dot com")
    assert a._booking_email == "billing@blueskylogistics.com"
    assert "billing@blueskylogistics.com" in done
    assert not a._email_is_new
    assert repo.carrier_emails("DOT1000001") == before     # no duplicate row


def test_new_email_is_appended_to_the_carrier_file(repo):
    """An address we've never seen is added — the file grows, and the addresses
    already on it are kept, not overwritten."""
    before = repo.carrier_emails("DOT1000001")
    a, _ = _to_email_step(repo)
    done = a.handle("booking at blue sky freight dot com")
    assert a._booking_email == "booking@blueskyfreight.com"
    assert a._email_is_new
    assert "booking@blueskyfreight.com" in done
    after = repo.carrier_emails("DOT1000001")
    assert set(before) < set(after)                        # kept, plus the new one
    assert "booking@blueskyfreight.com" in after
    assert repo.get_carrier("123456").contact_emails == after


def test_carrier_can_point_at_the_address_on_file(repo):
    """"Just use the one you've got" is a valid answer when we actually have one."""
    a, _ = _to_email_step(repo)
    done = a.handle("just use the one you have on file")
    assert a._booking_email in repo.carrier_emails("DOT1000001")
    assert a._booking_email in done
    assert a.summary()["outcome"] == "booked"


def test_no_usable_email_is_asked_once_then_flagged(repo):
    """Two tries, no address: book the load but never invent one, and leave a
    note so the con isn't sent blind."""
    a, _ = _to_email_step(repo)
    second = a.handle("uh, hang on a sec")                 # nothing usable
    assert "email" in second.lower()                       # asked once more
    assert "@" not in second
    done = a.handle("I'll have to dig it up")
    assert a.summary()["outcome"] == "booked"
    assert a._booking_email is None
    assert "@" not in done                                 # never fabricates an address
    conn = repo._db.connect()
    notes = conn.execute("SELECT note FROM call_notes").fetchall()
    conn.close()
    assert any("NOT CAPTURED" in n["note"] for n in notes)


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
