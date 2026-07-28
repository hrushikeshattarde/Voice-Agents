"""
`TransportProRepository` — the seam, against the fake API.

Reads come from Transport Pro; the call audit trail stays in SQLite. These tests
cover the decisions the repository makes on the way through, especially the ones
that keep the agent from saying something untrue: a load that isn't on the board,
a search that answered with the wrong load, and an API that didn't answer at all.
"""

import httpx
import pytest

from lanevoice.domain.errors import SourceUnavailable
from lanevoice.domain.models import AuthorityStatus
from tests.transportpro_fake import FakeTransportPro, board, repository
from tests.transportpro_payloads import (
    CARRIER_STATUS_ACTIVE,
    CARRIER_STATUS_INACTIVE,
    CONTACT_SEARCH,
    EMPTY_SEARCH,
    LOAD_DETAIL_WAYPOINTS,
    record_for,
    search_available_for,
)

LOAD_ID = "1303369"


@pytest.fixture
def fake():
    return FakeTransportPro()


def _repo(fake, repo, **overrides):
    return repository(fake, repo, **overrides)


# --------------------------------------------------------------------------- #
# Loads
# --------------------------------------------------------------------------- #
def test_a_posted_load_is_found_and_bookable(fake, repo):
    board(fake, record_for(int(LOAD_ID)))
    load = _repo(fake, repo).get_load(LOAD_ID)
    assert load.load_id == LOAD_ID
    assert load.is_bookable and load.is_quotable
    assert (load.open_rate, load.ceiling_rate) == (1600, 1800)
    assert load.origin == "Nashville, TN"


def test_the_load_number_is_confirmed_against_what_came_back(fake, repo):
    """Asked for one load, got another. Pitching it would be the worst possible
    kind of helpful, so it is dropped rather than sold."""
    fake.json(f"/load/{LOAD_ID}", record_for(1303370))
    assert _repo(fake, repo).get_load(LOAD_ID) is None


def test_a_load_that_exists_but_is_not_posted_is_reported_as_such(fake, repo):
    """Not on the board, but real — the agent says "it isn't posted", which is a
    different and true sentence from "there's no such load"."""
    fake.json("/load/1303298", LOAD_DETAIL_WAYPOINTS)
    load = _repo(fake, repo).get_load("1303298")
    assert load is not None
    assert not load.is_posted and not load.is_bookable


def test_a_load_nobody_has_heard_of_is_none(fake, repo):
    assert _repo(fake, repo).get_load("9999999") is None      # 404


def test_an_api_failure_is_not_reported_as_no_such_load(fake, repo):
    """The distinction this exception exists for. A 500 must never reach the
    carrier as "that load isn't on our board"."""
    fake.on(f"/load/{LOAD_ID}",
            httpx.Response(500, text="boom"), httpx.Response(500, text="boom"))
    with pytest.raises(SourceUnavailable, match="load lookup"):
        _repo(fake, repo).get_load(LOAD_ID)


def test_open_loads_are_capped_because_they_get_read_out_loud(fake, repo):
    board(fake, *[record_for(1303300 + n) for n in range(20)])
    loads = _repo(fake, repo, transport_pro_max_offered_loads=3).open_loads()
    assert len(loads) == 3


def test_open_loads_asks_the_api_for_posted_ready_to_dispatch_only(fake, repo):
    """The two conditions go server-side here, because `/load/search` actually
    takes them — and are still re-checked on every record that comes back."""
    board(fake, record_for(1303369))
    _repo(fake, repo).open_loads()

    params = fake.calls("/load/search")[0].url.params
    assert params["isPosted"] == "true"
    assert params["loadStatus"] == "ready to dispatch"
    assert params["pickupDateStart"] and params["pickupDateEnd"]


def test_open_loads_does_not_pin_a_status_when_several_are_configured(fake, repo):
    """One filter value can't express "either of these", so the filter is left off
    and the check happens on the records instead. Better a wider search than a
    silently wrong one."""
    board(fake, record_for(1303369))
    _repo(fake, repo,
          transport_pro_open_load_statuses="ready to dispatch, available").open_loads()
    params = fake.calls("/load/search")[0].url.params
    assert "loadStatus" not in params
    assert params["isPosted"] == "true"


# --------------------------------------------------------------------------- #
# Sellability: only Ready To Dispatch, only with posting on
# --------------------------------------------------------------------------- #
def test_a_load_that_is_not_ready_to_dispatch_is_not_sellable(fake, repo):
    board(fake, record_for(int(LOAD_ID), load_status="Available"))
    load = _repo(fake, repo).get_load(LOAD_ID)
    assert load is not None                  # we found it, and say so accurately
    assert not load.is_bookable
    assert load.status.value == "not_ready"


def test_a_load_with_posting_switched_off_is_not_sellable(fake, repo):
    board(fake, record_for(int(LOAD_ID), postingInfo={"isPosted": False}))
    load = _repo(fake, repo).get_load(LOAD_ID)
    assert load.status.value == "open"        # released...
    assert not load.is_posted                 # ...but not on the board
    assert not load.is_bookable


def test_the_status_setting_parses_the_way_an_operator_would_write_it():
    from tests.transportpro_fake import settings as _settings

    assert _settings().open_load_statuses == frozenset({"ready to dispatch"})
    assert _settings(
        transport_pro_open_load_statuses="Ready To Dispatch, AVAILABLE , Ready_To-Go"
    ).open_load_statuses == frozenset(
        {"ready to dispatch", "available", "ready to go"})
    # An empty setting sells nothing rather than everything.
    assert _settings(transport_pro_open_load_statuses=" , ").open_load_statuses == \
        frozenset()


def test_configured_statuses_are_what_the_repository_enforces(fake, repo):
    board(fake, record_for(int(LOAD_ID), load_status="Available"))
    tp = _repo(fake, repo,
               transport_pro_open_load_statuses="ready to dispatch, available")
    assert tp.get_load(LOAD_ID).is_bookable


def test_the_open_list_only_offers_loads_the_agent_can_sell(fake, repo):
    """These numbers get read out to a carrier, so an unsellable one in here is a
    load we offered and then refused."""
    payload = search_available_for(1)
    template = payload["results"][0]
    payload["results"] = [
        dict(template, load_id=1, load_status="Ready To Dispatch"),
        dict(template, load_id=2, load_status="Available"),          # not released
        dict(template, load_id=3, load_status="Covered"),            # gone
        dict(template, load_id=4, load_status="Ready To Dispatch",
             postingInfo={"isPosted": False}),                       # posting off
        dict(template, load_id=5, load_status="Ready To Dispatch"),
    ]
    board(fake, *payload["results"])
    assert [load.load_id for load in _repo(fake, repo).open_loads()] == ["1", "5"]


def test_the_cap_counts_sellable_loads_not_records(fake, repo):
    """A board full of unsellable loads must not crowd the good ones out of the
    list the agent reads aloud."""
    payload = search_available_for(1)
    template = payload["results"][0]
    payload["results"] = (
        [dict(template, load_id=100 + n, load_status="Covered") for n in range(10)]
        + [dict(template, load_id=200 + n, load_status="Ready To Dispatch")
           for n in range(4)]
    )
    board(fake, *payload["results"])
    loads = _repo(fake, repo, transport_pro_max_offered_loads=3).open_loads()
    assert [load.load_id for load in loads] == ["200", "201", "202"]


def test_unquotable_loads_are_not_offered_in_the_open_list(fake, repo):
    """A load with no board rate can't be sold, so it isn't read out as an option."""
    payload = search_available_for(1303369)
    payload["results"] = [
        dict(payload["results"][0], load_id=1,
             carrier_sales_data={"load_board_rate": None, "max_buy": None}),
        dict(payload["results"][0], load_id=2),
    ]
    board(fake, *payload["results"])
    assert [load.load_id for load in _repo(fake, repo).open_loads()] == ["2"]


def test_loads_are_cached_briefly_so_one_call_is_not_many_round_trips(fake, repo):
    board(fake, record_for(int(LOAD_ID)))
    tp = _repo(fake, repo, transport_pro_load_cache_seconds=60)
    tp.get_load(LOAD_ID)
    tp.get_load(LOAD_ID)
    assert len(fake.calls(f"/load/{LOAD_ID}")) == 1


# --------------------------------------------------------------------------- #
# Carriers
# --------------------------------------------------------------------------- #
def test_an_active_carrier_clears(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    carrier = _repo(fake, repo).get_carrier("MC 123456")
    assert carrier.authority_status == AuthorityStatus.ACTIVE
    assert carrier.authority_status.can_haul
    assert carrier.carrier_id == "13167"


def test_an_inactive_carrier_does_not(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_INACTIVE)
    carrier = _repo(fake, repo).get_carrier("555444")
    assert not carrier.authority_status.can_haul


def test_mc_is_tried_first_then_dot(fake, repo):
    """The caller rarely says which one they read out, so both are tried — MC
    first, because that is what a carrier volunteers on a sales call."""
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        if "mc_number" in request.url.params:
            return httpx.Response(200, json=EMPTY_SEARCH)      # no MC match
        return httpx.Response(200, json=CARRIER_STATUS_ACTIVE)

    fake.on("/voiceai/carrier_status", handler)
    assert _repo(fake, repo).get_carrier("1000001") is not None
    assert list(calls[0]) == ["mc_number"]
    assert list(calls[1]) == ["dot_number"]


def test_too_few_digits_is_not_looked_up_at_all(fake, repo):
    assert _repo(fake, repo).get_carrier("12") is None
    assert fake.calls("carrier_status") == []


def test_an_unknown_carrier_is_none_and_the_miss_is_remembered(fake, repo):
    fake.json("/voiceai/carrier_status", EMPTY_SEARCH)
    tp = _repo(fake, repo, transport_pro_carrier_cache_seconds=60)
    assert tp.get_carrier("000000") is None
    assert tp.get_carrier("000000") is None
    # A caller reading a wrong number back twice must not cost two round trips.
    assert len(fake.calls("carrier_status")) == 2   # one MC probe + one DOT probe


def test_a_carrier_is_cached_under_every_number_they_might_repeat(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    tp = _repo(fake, repo, transport_pro_carrier_cache_seconds=60)
    assert tp.get_carrier("123456") is not None      # by MC
    assert tp.get_carrier("1000001") is not None     # same carrier, by DOT
    assert len(fake.calls("carrier_status")) == 1


def test_carrier_lookup_failure_is_surfaced_not_swallowed(fake, repo):
    fake.on("/voiceai/carrier_status",
            httpx.Response(502, text="bad gateway"),
            httpx.Response(502, text="bad gateway"))
    with pytest.raises(SourceUnavailable, match="carrier lookup"):
        _repo(fake, repo).get_carrier("123456")


def test_partial_digit_matching_is_not_supported_and_says_so(fake, repo):
    """No prefix search exists on this API. Returning nothing is what makes the
    agent fall back to asking again rather than confirming a guessed carrier."""
    assert _repo(fake, repo).carriers_matching_digits("1234") == []
    assert fake.requests == []


# --------------------------------------------------------------------------- #
# The address file behind the booking gate
# --------------------------------------------------------------------------- #
def test_addresses_come_from_contact_search_keyed_on_the_carrier_id(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.json("/contact/search", CONTACT_SEARCH)
    tp = _repo(fake, repo)
    carrier = tp.get_carrier("123456")

    assert tp.carrier_emails(carrier.usdot_number) == (
        "dispatch@blueskylogistics.com", "billing@blueskylogistics.com")
    assert tp.email_on_file(carrier.usdot_number, "Billing@BlueSkyLogistics.com")
    assert not tp.email_on_file(carrier.usdot_number, "someone@else.com")

    params = fake.calls("/contact/search")[0].url.params
    assert params["connectionRecordId"] == "13167"
    assert params["connnectionRecordType"] == "brokerCarrier"


def test_a_carrier_record_with_no_id_cannot_have_its_addresses_checked(fake, repo):
    """No `carrier_id` means `/contact/search` can't be called. Returning no
    addresses is the safe answer: the booking gate then refuses rather than
    accepting an address nobody verified."""
    fake.json("/voiceai/carrier_status",
              {k: v for k, v in CARRIER_STATUS_ACTIVE.items() if k != "carrier_id"})
    tp = _repo(fake, repo)
    carrier = tp.get_carrier("123456")
    assert tp.carrier_emails(carrier.usdot_number) == ()
    assert not tp.email_on_file(carrier.usdot_number, "dispatch@blueskylogistics.com")


def test_addresses_are_never_written_back_and_the_caller_is_told(fake, repo):
    """There is no create-contact endpoint. Returning False is what stops the
    agent claiming it saved an address it didn't."""
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    tp = _repo(fake, repo)
    assert tp.add_carrier_email("1000001", "new@carrier.com") is False


def test_contact_lookup_failure_does_not_silently_pass_the_gate(fake, repo):
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.on("/contact/search", httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"))
    tp = _repo(fake, repo)
    carrier = tp.get_carrier("123456")
    with pytest.raises(SourceUnavailable, match="contact lookup"):
        tp.email_on_file(carrier.usdot_number, "dispatch@blueskylogistics.com")


# --------------------------------------------------------------------------- #
# Write-backs
# --------------------------------------------------------------------------- #
def test_a_booking_is_posted_as_an_offer(fake, repo):
    board(fake, record_for(int(LOAD_ID)))
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.json(f"/voiceai/load/{LOAD_ID}/make_offer", {"offer_id": 42})
    tp = _repo(fake, repo)
    load, carrier = tp.get_load(LOAD_ID), tp.get_carrier("123456")

    assert tp.record_booking(load, carrier, 1750,
                             email="dispatch@blueskylogistics.com",
                             contact_name="Dispatch") is True
    body = fake.bodies("make_offer")[0]
    assert body["offer_amount"] == "1750"
    assert body["email"] == "dispatch@blueskylogistics.com"
    assert body["carrier_name"] == "Blue Sky Logistics LLC"
    assert body["mc_number"] == "123456"
    assert body["carrier_id"] == "13167"


def test_a_failed_booking_returns_false_rather_than_raising(fake, repo):
    """The agent turns a False here into a handoff. An exception escaping instead
    would surface as a generic error and lose the reason."""
    board(fake, record_for(int(LOAD_ID)))
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.on(f"/voiceai/load/{LOAD_ID}/make_offer",
            httpx.Response(500, text="boom"), httpx.Response(500, text="boom"))
    tp = _repo(fake, repo)
    load, carrier = tp.get_load(LOAD_ID), tp.get_carrier("123456")
    assert tp.record_booking(load, carrier, 1750, email="a@b.com") is False


def test_a_booking_with_no_way_to_reach_the_carrier_is_refused(fake, repo):
    board(fake, record_for(int(LOAD_ID)))
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    tp = _repo(fake, repo)
    load, carrier = tp.get_load(LOAD_ID), tp.get_carrier("123456")
    assert tp.record_booking(load, carrier, 1750) is False
    assert fake.calls("make_offer") == []


def test_notes_reach_the_load_and_a_failure_never_propagates(fake, repo):
    fake.json(f"/voiceai/load/{LOAD_ID}/add_note", {})
    tp = _repo(fake, repo)
    assert tp.post_load_note(LOAD_ID, "carrier called about this load") is True
    assert fake.bodies("add_note")[0]["content"] == "carrier called about this load"

    fake.on("/voiceai/load/999/add_note", httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"))
    assert tp.post_load_note("999", "note") is False    # logged, not raised


def test_the_audit_trail_still_lands_in_sqlite(fake, repo):
    """Transport Pro has no endpoint for a call record, so losing this would make
    a disputed booking unauditable."""
    from lanevoice.domain.models import OfferParty

    tp = _repo(fake, repo)
    tp.start_call("CALL-1")
    tp.log_offer("CALL-1", 1, OfferParty.AGENT, 1600)
    tp.log_note("CALL-1", "a note")
    tp.end_call("CALL-1", LOAD_ID, "1000001", "booked", [("agent", "hi")])

    conn = repo._db.connect()
    try:
        assert conn.execute("SELECT outcome FROM calls WHERE call_id='CALL-1'"
                            ).fetchone()["outcome"] == "booked"
        assert conn.execute("SELECT COUNT(*) FROM negotiation_offers"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM call_notes").fetchone()[0] == 1
    finally:
        conn.close()


def test_reps_are_local_because_they_are_transfer_targets(fake, repo):
    """A rep here is a name and a phone for a warm transfer, not a Transport Pro
    record — so the seeded table is still the right source."""
    tp = _repo(fake, repo)
    assert tp.available_rep() is not None
    assert fake.requests == []
