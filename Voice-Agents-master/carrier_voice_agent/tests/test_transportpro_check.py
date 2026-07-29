"""
`lanevoice-tpcheck` — the pre-flight tool.

Its job is to tell somebody holding fresh credentials whether the wiring works
and, crucially, whether the undocumented `carrier_status` payload is being read
correctly. So what these tests assert is that it says the RIGHT THING in the
cases that matter — especially the two silent-failure modes it exists to catch:
a status field this code can't find, and a load with no rate to open at.
"""

import httpx

from lanevoice.integrations.transportpro.check import _check_carrier, _check_load
from tests.transportpro_fake import FakeTransportPro, board, client
from tests.transportpro_payloads import (
    CARRIER_STATUS_ACTIVE,
    CARRIER_STATUS_INACTIVE,
    CARRIER_STATUS_NO_STATUS_FIELD,
    CONTACT_SEARCH,
    EMPTY_SEARCH,
    LOAD_DETAIL_WAYPOINTS,
    internal_contacts,
    record_for,
    user_record,
)

LOAD = "1303369"


def test_an_active_carrier_reports_clearly(capsys):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.json("/contact/search", CONTACT_SEARCH)
    with client(fake) as api:
        _check_carrier(api, "123456", raw=False)

    out = capsys.readouterr().out
    assert "found (matched on mc_number)" in out
    assert "Blue Sky Logistics LLC" in out
    assert "can haul for us   : YES" in out
    assert "2 address(es) on the account" in out


def test_an_inactive_carrier_is_reported_as_unable_to_haul(capsys):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_INACTIVE)
    fake.json("/contact/search", EMPTY_SEARCH)
    with client(fake) as api:
        _check_carrier(api, "555444", raw=False)

    out = capsys.readouterr().out
    assert "read as           : inactive" in out
    assert "can haul for us   : NO" in out


def test_an_unfindable_status_field_is_called_out_loudly(capsys):
    """The failure mode the tool exists for. A silent SUSPENDED here would send
    every caller to a human and look like a policy decision rather than a bug."""
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_NO_STATUS_FIELD)
    fake.json("/contact/search", CONTACT_SEARCH)
    with client(fake) as api:
        _check_carrier(api, "313131", raw=False)

    out = capsys.readouterr().out
    assert "NO STATUS FIELD FOUND" in out
    assert "_STATUS_KEYS" in out          # tells you exactly where to fix it


def test_a_carrier_with_no_id_warns_that_bookings_cannot_be_confirmed(capsys):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status",
              {k: v for k, v in CARRIER_STATUS_ACTIVE.items() if k != "carrier_id"})
    with client(fake) as api:
        _check_carrier(api, "123456", raw=False)
    assert "NO booking can be confirmed" in capsys.readouterr().out


def test_a_carrier_with_no_addresses_warns_the_gate_will_refuse(capsys):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.json("/contact/search", EMPTY_SEARCH)
    with client(fake) as api:
        _check_carrier(api, "123456", raw=False)
    assert "no email addresses on this carrier's account" in capsys.readouterr().out


def test_an_unknown_carrier_says_so(capsys):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", EMPTY_SEARCH)
    with client(fake) as api:
        _check_carrier(api, "000000", raw=False)
    assert "not found as an MC or a USDOT number" in capsys.readouterr().out


def test_a_posted_load_prints_the_floor_the_cap_and_the_notes(capsys):
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD)))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)

    out = capsys.readouterr().out
    assert "load 1303369: found" in out
    assert "Nashville, TN -> Miami, FL" in out
    assert "floor (opens at)  : $1600" in out
    assert "max buy (cap)     : $1800" in out
    assert "board notes       : test board comment" in out


def test_a_status_mismatch_is_the_first_thing_reported(capsys):
    """The likeliest reason a healthy board reads as empty. The tool has to name
    the value it saw and the env var that fixes it, or somebody spends an
    afternoon on it."""
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD), load_status="AVAILABLE"))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)

    out = capsys.readouterr().out
    assert "status on record  : 'AVAILABLE'" in out
    assert "statuses we sell  : ready to dispatch" in out
    assert "is NOT sellable" in out
    assert "TRANSPORT_PRO_OPEN_LOAD_STATUSES" in out


def test_a_sellable_status_and_posting_flag_both_report_ok(capsys):
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD), postingInfo={"isPosted": True}))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)

    out = capsys.readouterr().out
    assert "status is one the agent sells" in out
    assert "isPosted = true on the record" in out


def test_posting_switched_off_is_reported_as_a_blocker(capsys):
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD), postingInfo={"isPosted": False}))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)
    assert "isPosted = false" in capsys.readouterr().out


def test_a_missing_posting_flag_explains_why_the_load_reads_as_unposted(capsys):
    """`GET /load/{id}` serves any load, so a payload with no `postingInfo` is not
    evidence of being on the board. The tool has to say so, because "the agent
    won't offer this" with no reason given is the least useful output there is."""
    fake = FakeTransportPro()
    record = record_for(int(LOAD))
    del record["postingInfo"]
    board(fake, record)
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)
    out = capsys.readouterr().out
    assert "carries no isPosted field" in out
    assert "GET /load/{id} serves any load" in out


def test_a_load_with_no_rate_is_flagged_as_unquotable(capsys):
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD), carrier_sales_data={"load_board_rate": None,
                                                       "max_buy": None}))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)
    assert "no usable Load Board Rate" in capsys.readouterr().out


def test_an_unposted_load_is_distinguished_from_a_missing_one(capsys):
    """A load with posting off and one that doesn't exist say different things —
    the agent does too, so the pre-flight tool has to show the difference."""
    fake = FakeTransportPro()
    fake.json("/load/1303298", LOAD_DETAIL_WAYPOINTS)
    with client(fake) as api:
        _check_load(api, "1303298", raw=False)
    out = capsys.readouterr().out
    assert "found" in out
    assert "carries no isPosted field" in out      # -> reads as not posted

    fake = FakeTransportPro()                       # nothing routed -> 404
    with client(fake) as api:
        _check_load(api, "9999999", raw=False)
    assert "no such load" in capsys.readouterr().out


def test_raw_prints_the_payload_for_eyeballing(capsys):
    fake = FakeTransportPro()
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_NO_STATUS_FIELD)
    fake.json("/contact/search", CONTACT_SEARCH)
    with client(fake) as api:
        _check_carrier(api, "313131", raw=True)
    out = capsys.readouterr().out
    assert "raw carrier_status for 313131" in out
    assert "Unlabelled Carriers Inc" in out


def test_the_tool_never_writes_anything(capsys):
    """Read-only by contract: somebody debugging credentials must not accidentally
    post an offer on a real load."""
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD)))
    fake.json("/voiceai/carrier_status", CARRIER_STATUS_ACTIVE)
    fake.json("/contact/search", CONTACT_SEARCH)
    with client(fake) as api:
        _check_load(api, LOAD, raw=True)
        _check_carrier(api, "123456", raw=True)

    assert all(request.method == "GET" or request.url.path.endswith("/auth")
               for request in fake.requests)
    assert fake.calls("make_offer") == []
    assert fake.calls("add_note") == []
    assert fake.calls("add_carrier_capacity") == []


def test_a_carrier_status_shape_with_no_numbers_reports_a_mapping_gap(capsys):
    fake = FakeTransportPro()
    fake.on("/voiceai/carrier_status",
            httpx.Response(200, json={"companyName": "Anonymous", "status": "active"}))
    with client(fake) as api:
        _check_carrier(api, "123456", raw=False)
    assert "_MC_KEYS" in capsys.readouterr().out


def test_the_rep_a_caller_would_reach_is_reported(capsys):
    """The third silent failure: a handoff that works, to the wrong desk."""
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD), internalContacts=internal_contacts(
        ORDERTAKER=1000, CARRIERREP=2423)))
    fake.json("/user/2423", user_record(2423))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)

    out = capsys.readouterr().out
    assert "ORDERTAKER, CARRIERREP" in out
    assert "Lucas Piqueras" in out
    assert "asking for the rep on this load reaches them" in out
    # The extension can't travel in a SIP transfer, so it is called out.
    assert "extension 8754" in out


def test_a_load_with_no_carrier_rep_names_the_setting_to_fix(capsys):
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD),
                           internalContacts=internal_contacts(ORDERTAKER=1000)))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)

    out = capsys.readouterr().out
    assert "no carrier sales rep on this load" in out
    assert "TRANSPORT_PRO_CARRIER_REP_CONTACT_TYPES" in out


def test_a_rep_with_no_dialable_number_is_called_out(capsys):
    fake = FakeTransportPro()
    board(fake, record_for(int(LOAD),
                           internalContacts=internal_contacts(CARRIERREP=2423)))
    fake.json("/user/2423", user_record(2423, phoneNumbers=[
        {"type": "FAX", "value": "260-220-8703"}]))
    with client(fake) as api:
        _check_load(api, LOAD, raw=False)

    out = capsys.readouterr().out
    assert "no dialable number on their user record" in out
