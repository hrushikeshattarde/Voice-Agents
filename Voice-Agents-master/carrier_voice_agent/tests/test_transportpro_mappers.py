"""
Transport Pro JSON -> domain models, against the collection's own payloads.

These are the tests that matter most for this integration, because everything
downstream — what gets said to a carrier, what the agent opens the negotiation at,
whether a carrier is turned away — is decided from what comes out of here.
"""

import copy

from lanevoice.domain.models import AuthorityStatus, LoadStatus
from lanevoice.integrations.transportpro.client import _records
from lanevoice.integrations.transportpro.mappers import (
    contact_emails,
    map_carrier,
    map_load,
    normalize_status,
)
from tests.transportpro_payloads import (
    CARRIER_STATUS_ACTIVE,
    CARRIER_STATUS_INACTIVE,
    CARRIER_STATUS_LIVE_ACTIVE,
    CARRIER_STATUS_LIVE_FAIL,
    CARRIER_STATUS_LIVE_REVIEW,
    CARRIER_STATUS_NO_STATUS_FIELD,
    CARRIER_STATUS_SUSPENDED_ENVELOPED,
    CARRIER_STATUS_UNKNOWN_WORD,
    CONTACT_SEARCH,
    CONTACT_SEARCH_LIVE,
    LOAD_DETAIL_BOOKABLE,
    LOAD_DETAIL_UNPOSTED,
    LOAD_DETAIL_WAYPOINTS,
    READY,
    SEARCH_AVAILABLE,
)

RECORD = SEARCH_AVAILABLE["results"][0]


def _load(*, posted=True, open_statuses=None, **overrides):
    """Map the collection's record, set to the status the desk sells.

    `RECORD` is verbatim from the API collection and says `"AVAILABLE"`, which the
    agent does NOT sell by default — see the sellability tests below. Everything
    else here is about rates, lanes and notes, so the default is a load that
    passes the gate.
    """
    record = copy.deepcopy(RECORD)
    record["load_status"] = READY
    record.update(overrides)
    return map_load(record, posted=posted, open_statuses=open_statuses)


# --------------------------------------------------------------------------- #
# Loads
# --------------------------------------------------------------------------- #
def test_the_lane_is_the_ends_of_the_run_not_the_middle_stop():
    """Nashville -> Miami. Marathon is a middle drop and the carrier is being
    sold the ends of the run, so `Final Delivery` wins over it."""
    load = _load()
    assert load.load_id == "1303369"
    assert load.origin == "Nashville, TN"
    assert load.destination == "Miami, FL"
    assert load.status == LoadStatus.OPEN
    assert load.is_bookable


def test_rates_map_to_the_engines_floor_and_ceiling():
    """`load_board_rate` is what the agent opens at and `max_buy` is the cap it
    will never exceed — the exact split NegotiationEngine already expects."""
    load = _load()
    assert load.open_rate == 1600      # anchor here
    assert load.ceiling_rate == 1800   # never above this
    assert load.is_quotable
    assert load.fraud_low_rate == 800  # default ratio of the board rate


def test_board_notes_become_the_requirements_read_to_the_carrier():
    assert _load().notes == "test board comment"


def test_reference_information_is_flattened_into_spoken_facts():
    load = _load()
    assert load.equipment == "Van"
    assert load.miles == 1142
    assert load.commodity == "Aircraft Metal"
    facts = load.facts()
    assert "Van" in facts and "1,142" in facts and "Aircraft Metal" in facts


def test_appointment_windows_are_read_as_wall_clock_with_the_zone():
    """17:22Z-18:00Z with a zone of AST is spoken as 5:22 PM to 6 PM AST — the
    times as written, never shifted. See the mapper's module docstring."""
    load = _load()
    assert load.pickup_date == "2022-03-25"
    assert load.pickup_window == "5:22 PM to 6 PM AST"


def test_a_window_that_reads_backwards_is_never_spoken_as_a_window():
    """The delivery stop is 05:00Z to 20:00Z, which as a window is 5 AM to 8 PM —
    fine. Invert it and the mapper must fall back to the start alone rather than
    reading a nonsense range to a driver."""
    record = copy.deepcopy(RECORD)
    final = record["shipment_information"]["waypoints"][-1]
    final["appointment_date"] = {
        "start": "2022-03-26T20:00:00Z",
        "end": "2022-03-26T05:00:00Z",
        "timezone": "CST",
    }
    load = map_load(record, posted=True)
    assert load.delivery_window == "8 PM appointment CST"
    assert " to " not in load.delivery_window


def test_missing_appointment_spelled_false_is_not_a_date():
    """The middle stop's appointment is literally `false`. Nothing about it may
    surface as a date, a window, or the string "False"."""
    load = _load()
    for value in (load.pickup_date, load.pickup_window,
                  load.delivery_date, load.delivery_window):
        assert "False" not in str(value)
    assert "False" not in load.facts()


def test_a_load_with_no_published_rate_is_not_quotable():
    """Posted and open, but no board rate. The agent must not invent an anchor."""
    load = _load(carrier_sales_data={"load_board_rate": None, "max_buy": None})
    assert load.is_bookable          # it IS on the board
    assert not load.is_quotable      # but there is nothing to open at


def test_a_cap_below_the_anchor_never_produces_an_inverted_range():
    load = _load(carrier_sales_data={"load_board_rate": 1600, "max_buy": 1200})
    assert load.open_rate == 1600
    assert load.ceiling_rate == 1600      # clamped up, not left inverted
    assert load.is_quotable


def test_a_cap_with_no_anchor_is_not_quotable():
    """A Max Buy is not an anchor. Opening at the cap is the worst opening there
    is — the carrier simply accepts it and the desk has paid its maximum with no
    chance of having done better — so this goes to a rep, like a load with no
    rates at all. Three loads on the live board are in this state."""
    load = _load(carrier_sales_data={"load_board_rate": None, "max_buy": 1800})
    assert load.ceiling_rate == 1800     # the cap is still known and still binding
    assert load.open_rate == 0
    assert not load.is_quotable


def test_rates_typed_as_strings_still_parse():
    load = _load(carrier_sales_data={"load_board_rate": "1,600", "max_buy": "$1800"})
    assert load.open_rate == 1600 and load.ceiling_rate == 1800


def test_a_load_reached_only_by_detail_is_not_posted():
    """`search_available` is the board. Coming back from load detail instead means
    the load exists but cannot be sold, and the agent says so."""
    record = LOAD_DETAIL_WAYPOINTS["results"][0]
    load = map_load(record, posted=False)
    assert load.load_id == "1303298"
    assert not load.is_posted
    assert not load.is_bookable
    # Its stops live under dispatch_information, and are still found.
    assert load.destination == "Slidell, LA"


def test_a_record_with_no_load_id_maps_to_nothing():
    assert map_load({"load_status": "AVAILABLE"}, posted=True) is None


# --------------------------------------------------------------------------- #
# Sellability: Ready To Dispatch AND posting on. Both checked on the record.
# --------------------------------------------------------------------------- #
def test_ready_to_dispatch_is_sellable():
    load = _load(load_status="Ready To Dispatch")
    assert load.status == LoadStatus.OPEN
    assert load.is_bookable


def test_status_spelling_and_punctuation_do_not_matter():
    for spelling in ("Ready To Dispatch", "ready to dispatch", "READY_TO_DISPATCH",
                     "Ready-To-Dispatch", "  ready  to   dispatch  "):
        assert _load(load_status=spelling).status == LoadStatus.OPEN, spelling


def test_the_collections_own_status_is_rejected_by_default():
    """`search_available`'s saved example says "AVAILABLE", and the desk sells only
    Ready To Dispatch — so out of the box that load is NOT offered. This is the
    mismatch `TRANSPORT_PRO_OPEN_LOAD_STATUSES` and `lanevoice-tpcheck` exist for."""
    load = _load(load_status="AVAILABLE")
    assert load.status == LoadStatus.NOT_READY
    assert not load.is_bookable


def test_configuring_extra_statuses_makes_them_sellable():
    load = _load(load_status="AVAILABLE",
                 open_statuses=frozenset({"ready to dispatch", "available"}))
    assert load.status == LoadStatus.OPEN
    assert load.is_bookable


def test_a_status_meaning_somebody_else_has_it_reads_as_covered():
    """Told apart from merely-not-ready so the agent can say "already covered"
    only when that is actually true."""
    for status in ("Covered", "Booked", "Dispatched", "In Transit", "Delivered"):
        assert _load(load_status=status).status == LoadStatus.COVERED, status


def test_a_status_that_is_simply_not_released_reads_as_not_ready():
    for status in ("Available", "Planned", "On Hold", "Quoted", "Needs Appointment"):
        assert _load(load_status=status).status == LoadStatus.NOT_READY, status


def test_cancelled_is_its_own_thing():
    assert _load(load_status="Cancelled").status == LoadStatus.CANCELLED


def test_a_missing_status_is_not_sellable():
    assert _load(load_status=None).status == LoadStatus.NOT_READY
    assert _load(load_status=False).status == LoadStatus.NOT_READY


def test_the_status_is_found_under_any_of_the_apis_spellings():
    record = copy.deepcopy(RECORD)
    del record["load_status"]
    record["loadStatus"] = READY                     # /load/search spelling
    assert map_load(record, posted=True).status == LoadStatus.OPEN

    record = copy.deepcopy(RECORD)
    del record["load_status"]
    record["status"] = {"loadStatus": READY}         # nested
    assert map_load(record, posted=True).status == LoadStatus.OPEN


def test_is_posted_false_blocks_a_ready_to_dispatch_load():
    """Both conditions, not either. A released load with posting switched off is
    not on the board and must not be sold."""
    load = _load(postingInfo={"isPosted": False, "comments": None})
    assert load.status == LoadStatus.OPEN
    assert not load.is_posted
    assert not load.is_bookable


def test_is_posted_true_on_the_record_is_honoured():
    load = _load(postingInfo={"isPosted": True, "loadBoardRate": 1250})
    assert load.is_posted and load.is_bookable


def test_is_posted_beats_the_endpoint_it_came_from():
    """An explicit flag on the record wins over provenance in both directions."""
    assert not _load(posted=True, postingInfo={"isPosted": False}).is_posted
    assert _load(posted=False, postingInfo={"isPosted": True}).is_posted


def test_a_record_with_no_posting_flag_falls_back_to_the_endpoint():
    """The collection's `search_available` example carries no posting field at all.
    Requiring one would reject the whole posted board, so provenance stands in:
    that endpoint IS "Search Posted Loads"."""
    assert "postingInfo" not in RECORD and "isPosted" not in RECORD
    assert _load(posted=True).is_posted           # from search_available
    assert not _load(posted=False).is_posted      # from load detail only


def test_posting_flags_typed_as_strings_still_work():
    assert not _load(postingInfo={"isPosted": "false"}).is_posted
    assert _load(postingInfo={"isPosted": "true"}).is_posted
    assert not _load(isPosted="No").is_posted     # flat, not nested


def test_normalize_status_is_the_comparison_used_everywhere():
    assert normalize_status(" Ready_To-Dispatch ") == "ready to dispatch"
    assert normalize_status(None) == ""
    assert normalize_status(False) == ""


# --------------------------------------------------------------------------- #
# Real production payloads from GET /load/{id}
#
# A different shape from the Voice AI feed in almost every field. Against the
# pre-existing mapper these produced a load with no lane, no rate and no
# equipment — safe, because unquotable loads go to a rep, but useless.
# --------------------------------------------------------------------------- #
def test_the_two_real_loads_differ_only_by_isposted_and_that_decides_it():
    """The rule, stated in production data: same `Ready To Dispatch`, opposite
    `isPosted`, opposite outcomes."""
    unposted = map_load(LOAD_DETAIL_UNPOSTED, posted=True)
    bookable = map_load(LOAD_DETAIL_BOOKABLE, posted=True)

    assert unposted.status == LoadStatus.OPEN and bookable.status == LoadStatus.OPEN
    assert unposted.is_posted is False and bookable.is_posted is True
    assert not unposted.is_bookable
    assert bookable.is_bookable


def test_rates_come_from_posting_info_on_the_load_endpoints():
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    assert load.open_rate == 5025      # postingInfo.loadBoardRate
    assert load.ceiling_rate == 5200   # postingInfo.maxBuy
    assert load.is_quotable


def test_the_customers_freight_charge_is_never_used_as_a_carrier_rate():
    """`billingInfo.charges.totalFreight` is what the customer pays us — 6092 on
    this load against a 5025 board rate. Quoting it would hand the carrier our
    entire margin, so nothing may fall back to it."""
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    for amount in (6092, 679.84, 6771.84):
        assert load.open_rate != amount
        assert load.ceiling_rate != amount
        assert load.fraud_low_rate != amount

    # And a load with no published rate stays unquotable rather than reaching for
    # the customer's 2890.
    unposted = map_load(LOAD_DETAIL_UNPOSTED, posted=True)
    assert unposted.open_rate == 0 and not unposted.is_quotable


def test_the_lane_comes_from_sh_and_cn_stops_with_nested_locations():
    assert map_load(LOAD_DETAIL_UNPOSTED, posted=True).origin == "Richmond, VA"
    assert (map_load(LOAD_DETAIL_UNPOSTED, posted=True).destination
            == "Traverse City, MI")
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    # SIKESTON is shouted in the feed; it should not be shouted at the carrier.
    assert load.origin == "Sikeston, MO"
    assert load.destination == "Vineland, NJ"


def test_appointment_markers_keep_the_date_the_operator_typed():
    """Every stop on both loads has `open == close` and `Not Required`, so the
    stamp is a date marker. The Chicago pickup is the one that catches a naive
    conversion: `2026-07-30T03:00:00Z` in America/Chicago is July TWENTY-NINTH."""
    unposted = map_load(LOAD_DETAIL_UNPOSTED, posted=True)
    bookable = map_load(LOAD_DETAIL_BOOKABLE, posted=True)

    assert unposted.pickup_date == "2026-07-29"
    assert unposted.delivery_date == "2026-07-30"
    assert bookable.pickup_date == "2026-07-30"     # not 07-29
    assert bookable.delivery_date == "2026-07-31"


def test_no_appointment_stops_are_described_not_given_a_clock_time():
    """Midnight-local is how these record "no appointment". Reading it back as
    "12 AM" would send a driver to a dock in the middle of the night."""
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    assert load.pickup_window == "no appointment needed, first come first served"
    assert "12 AM" not in load.facts()
    assert "AM" not in (load.pickup_window or "")


def test_reference_object_fields_are_read():
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    assert load.equipment == "Reefer"
    assert load.miles == 958          # 958.58 rounded down to a spoken figure
    assert load.commodity == "Ice Cream"
    assert load.weight_lbs == 38309
    assert load.pieces == 5040


def test_the_reefer_setpoint_is_spoken():
    """An ice cream load at minus twenty. A driver who agrees without hearing the
    temperature has agreed to something else."""
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    assert load.temperature == "-20 F"
    assert "-20 F" in load.facts()
    assert map_load(LOAD_DETAIL_UNPOSTED, posted=True).temperature is None


def test_a_piece_count_of_zero_is_not_read_out():
    """`numberOfPieces: 0` means nobody filled it in, not "zero pallets"."""
    load = map_load(LOAD_DETAIL_UNPOSTED, posted=True)
    assert load.pieces is None
    assert "Pieces" not in load.facts()


def test_a_trailer_length_is_not_announced_as_freight_dimensions():
    """`dimensions.length: "53.00"` with no width or height is the trailer. Every
    dry van has one; saying it sounds like a spec the carrier has to match."""
    assert map_load(LOAD_DETAIL_UNPOSTED, posted=True).dimensions is None


def test_cargo_dimensions_are_announced_when_somebody_measured_them():
    import copy
    record = copy.deepcopy(LOAD_DETAIL_UNPOSTED)
    record["reference"]["dimensions"] = {"length": "20", "width": "4", "height": "3"}
    assert map_load(record, posted=True).dimensions == "20 ft long x 4 ft wide x 3 ft high"


# --- Notes: the stop instructions, cleaned up enough to say out loud --------- #
def test_shipper_instructions_become_the_requirements_gate():
    """This is the note that matters: a BOL rule and a site check-in procedure.
    Reaching `notes` is what routes the call through CHECK_REQUIREMENTS, so the
    carrier is asked whether they can comply before any rate is discussed."""
    notes = map_load(LOAD_DETAIL_UNPOSTED, posted=True).notes
    assert notes.startswith("At pickup: ")
    assert "CARRIER MUST SEND BOL PRIOR TO LEAVING THE SHIPPER" in notes
    assert "trailer license plate number" in notes
    assert "inbound and outbound shipments" in notes


def test_html_never_reaches_the_carrier():
    notes = map_load(LOAD_DETAIL_UNPOSTED, posted=True).notes
    for markup in ("<br/>", "<br>", "<", ">", "&amp;", "&nbsp;"):
        assert markup not in notes


def test_operator_separator_runs_are_stripped():
    """"########################" is a divider typed into a notes box. Read
    aloud it is twenty-four hashes."""
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    assert "#" not in (load.notes or "")


def test_a_note_that_is_only_a_reference_number_is_not_a_requirement():
    """Both stops on the bookable load carry nothing but a BOL and a PO number.
    Treating those as requirements would make the agent ask a driver whether they
    can comply with "BOL # 0034850710"."""
    load = map_load(LOAD_DETAIL_BOOKABLE, posted=True)
    assert load.notes is None
    assert "0034850710" not in load.facts()


def test_delivery_instructions_are_labelled_separately():
    import copy
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["waypoints"][1]["notes"] = "Lumper fee applies, driver must not leave"
    notes = map_load(record, posted=True).notes
    assert "At delivery: Lumper fee applies" in notes


def test_posting_comments_are_read_as_board_notes():
    import copy
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["postingInfo"]["comments"] = "Team drivers preferred on this one"
    assert "Team drivers preferred" in map_load(record, posted=True).notes


# --------------------------------------------------------------------------- #
# The REAL carrier_status shape, captured from the live tenant
#
# `/voiceai/carrier_status` has no example response in the API collection, so
# these are the tests that pin what actually comes back — including the fact that
# its vocabulary (ACTIVE / FAIL / REVIEW) is nothing like the active/inactive/
# suspended wording the rest of the API uses.
# --------------------------------------------------------------------------- #
def test_the_live_active_verdict_clears_the_desk():
    record = _records(CARRIER_STATUS_LIVE_ACTIVE)[0]
    carrier = map_carrier(record)
    assert carrier.raw_authority_status == "ACTIVE"
    assert carrier.authority_status == AuthorityStatus.ACTIVE
    assert carrier.authority_status.can_haul
    assert carrier.carrier_id == "13167"
    assert carrier.mc_number == "556949"


def test_the_live_fail_verdict_is_a_definite_no():
    """DOT 2999221 on the live tenant. FAIL is a settled answer, so the carrier is
    told they don't meet the requirements rather than being put in a queue."""
    record = _records(CARRIER_STATUS_LIVE_FAIL)[0]
    carrier = map_carrier(record)
    assert carrier.raw_authority_status == "FAIL"
    assert carrier.authority_status == AuthorityStatus.SUSPENDED
    assert not carrier.authority_status.can_haul
    assert carrier.authority_status.is_definite
    assert carrier.legal_name == "Creed Transport Inc"


def test_the_live_review_verdict_is_not_a_refusal():
    """REVIEW means onboarding hasn't finished — the carrier has failed nothing.
    Telling them they don't meet our requirements would be untrue, and it is the
    kind of thing a carrier repeats to other brokers."""
    record = _records(CARRIER_STATUS_LIVE_REVIEW)[0]
    carrier = map_carrier(record)
    assert carrier.raw_authority_status == "REVIEW"
    assert carrier.authority_status == AuthorityStatus.PENDING
    assert not carrier.authority_status.can_haul     # still no load, still no rate
    assert not carrier.authority_status.is_definite  # ...but not a decline either


def test_the_carrier_record_envelope_is_unwrapped():
    """The carrier is inside a `carrier_record` list, with the onboarding team's
    contact details sitting at the same depth. Unwrapped, so an onboarding phone
    number can never be read as a carrier attribute."""
    records = _records(CARRIER_STATUS_LIVE_ACTIVE)
    assert len(records) == 1
    assert "carrier_onboarding_team" not in records[0]
    assert records[0]["carrier_name"] == "Blue Sky Logistics LLC"


def test_the_live_address_state_is_still_not_a_status():
    """"Illinois" sits in a `state` field right next to `status`."""
    carrier = map_carrier(_records(CARRIER_STATUS_LIVE_ACTIVE)[0])
    assert carrier.raw_authority_status == "ACTIVE"     # not "Illinois"
    assert carrier.authority_status.can_haul


# --------------------------------------------------------------------------- #
# Carriers — only ACTIVE clears
# --------------------------------------------------------------------------- #
def test_active_carrier_maps_and_carries_its_transport_pro_id():
    carrier = map_carrier(CARRIER_STATUS_ACTIVE)
    assert carrier.authority_status == AuthorityStatus.ACTIVE
    assert carrier.authority_status.can_haul
    assert carrier.legal_name == "Blue Sky Logistics LLC"
    assert carrier.mc_number == "123456"
    assert carrier.usdot_number == "1000001"
    assert carrier.carrier_id == "13167"     # needed for /contact/search
    assert carrier.authority_reported


def test_inactive_carrier_cannot_haul():
    carrier = map_carrier(CARRIER_STATUS_INACTIVE)
    assert carrier.authority_status == AuthorityStatus.INACTIVE
    assert not carrier.authority_status.can_haul
    assert carrier.raw_authority_status == "Inactive"


def test_suspended_carrier_in_a_results_envelope_with_camelcase_keys():
    record = CARRIER_STATUS_SUSPENDED_ENVELOPED["results"][0]
    carrier = map_carrier(record)
    assert carrier.authority_status == AuthorityStatus.SUSPENDED
    assert not carrier.authority_status.can_haul
    assert carrier.legal_name == "Ghost Carrier LLC"
    assert carrier.carrier_id == "13169"


def test_an_unrecognised_status_fails_closed_to_suspended():
    """"Do Not Use" is not a word we map. It must never read as ACTIVE."""
    carrier = map_carrier(CARRIER_STATUS_UNKNOWN_WORD)
    assert carrier.authority_status == AuthorityStatus.SUSPENDED
    assert not carrier.authority_status.can_haul
    assert carrier.authority_reported          # it DID tell us something


def test_status_field_we_cannot_find():
    """No status field on the record. Fails closed for safety, but flagged as
    unreported so the verification service sends it to a human instead of telling
    a possibly-fine carrier they don't meet our requirements."""
    carrier = map_carrier(CARRIER_STATUS_NO_STATUS_FIELD)
    assert carrier.authority_status == AuthorityStatus.SUSPENDED
    assert carrier.authority_reported is False
    assert carrier.raw_authority_status is None


def test_the_carriers_address_state_is_never_read_as_a_status():
    """`state: TN` is where they are, not what they are. Reading it as a status
    would suspend every carrier in Tennessee."""
    carrier = map_carrier({**CARRIER_STATUS_NO_STATUS_FIELD, "state": "TN"})
    assert carrier.raw_authority_status is None


def test_a_boolean_flag_instead_of_a_status_word():
    active = map_carrier({"mc_number": "1", "company_name": "X", "is_active": True})
    dormant = map_carrier({"mc_number": "2", "company_name": "Y", "is_active": False})
    assert active.authority_status == AuthorityStatus.ACTIVE
    assert dormant.authority_status == AuthorityStatus.INACTIVE
    assert active.authority_reported and dormant.authority_reported


def test_a_carrier_with_no_number_at_all_maps_to_nothing():
    assert map_carrier({"company_name": "Nameless", "status": "active"}) is None


def test_an_mc_only_carrier_gets_a_stable_key():
    """USDOT is the key everywhere downstream. A carrier who only gave an MC must
    not collapse onto an empty string shared with every other such carrier."""
    carrier = map_carrier({"mc_number": "343195", "company_name": "MC Only",
                           "status": "active"})
    assert carrier.usdot_number == "MC343195"


def test_missing_insurance_field_does_not_decline_by_default():
    """The desk gate is authority. A payload silent on insurance must not turn
    every caller into an insurance failure."""
    record = {k: v for k, v in CARRIER_STATUS_ACTIVE.items()
              if k != "insurance_on_file"}
    assert map_carrier(record).insurance_on_file is True
    assert map_carrier(record, insurance_reported_only=True).insurance_on_file is False


# --------------------------------------------------------------------------- #
# Contacts — the address file the booking gate checks against
# --------------------------------------------------------------------------- #
def test_contact_search_addresses_are_lowercased_and_deduplicated():
    emails = contact_emails(CONTACT_SEARCH["results"])
    assert emails == ("dispatch@blueskylogistics.com",
                      "billing@blueskylogistics.com")


def test_the_live_nested_email_contacts_shape_is_found():
    """`emailContacts: [{"type": "MAIN", "value": "..."}]` — the real shape. A
    field-name match found the list and stopped there, so the booking gate saw no
    addresses for any live carrier and no call could ever be confirmed."""
    emails = contact_emails(CONTACT_SEARCH_LIVE["results"])
    assert emails == ("johnsontrucking@tds.net", "james@johnsontruckingtn.com")


def test_an_address_is_matched_by_shape_not_by_field_name():
    """Whatever the API calls the field, an @-shaped string in a contact record is
    an address. This is what keeps the gate working through a rename."""
    for record in (
        {"emailContacts": [{"type": "MAIN", "value": "a@b.com"}]},
        {"email": "a@b.com"},
        {"contactMethods": [{"kind": "EMAIL", "detail": "a@b.com"}]},
        {"primary": {"addresses": {"work": "A@B.COM"}}},
        {"notes": "reach dispatch at a@b.com any time"},
    ):
        assert contact_emails([record]) == ("a@b.com",), record


def test_phone_numbers_are_not_mistaken_for_addresses():
    """`phoneNumbers` uses the same `{type, value}` shape as `emailContacts`, so
    the two are only told apart by what the value looks like."""
    record = {
        "phoneNumbers": [{"type": "OFFICE", "value": "6157937833"}],
        "emailContacts": [{"type": "MAIN", "value": "dispatch@carrier.com"}],
    }
    assert contact_emails([record]) == ("dispatch@carrier.com",)


def test_contacts_with_no_address_contribute_nothing():
    assert contact_emails([{"name": "No Email", "phone": "615-555-0100"}]) == ()
    assert contact_emails([]) == ()


# --------------------------------------------------------------------------- #
# Carrier qualifications a load demands
#
# Every shape below was observed on the live tenant on 2026-08-12, across the 200
# posted loads in the next-7-days window. `requiredClassifications` was populated
# on 19 of them and `commodityValue` on 4, so the null case is the common one and
# has to mean "nothing extra required" rather than "we couldn't tell".
# --------------------------------------------------------------------------- #
def test_required_classifications_are_read_off_the_reference_object():
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["reference"]["requiredClassifications"] = ["Temperature Controlled"]
    load = map_load(record, posted=True)
    assert load.required_classifications == ("Temperature Controlled",)


def test_a_doubled_classification_is_deduplicated():
    """The live feed really does answer `["Critical Cargo", "Critical Cargo"]` —
    loads 2487956, 2487957 and 2518880 all did. A doubled entry would make any
    count-based check wrong."""
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["reference"]["requiredClassifications"] = ["Critical Cargo", "Critical Cargo"]
    assert map_load(record, posted=True).required_classifications == ("Critical Cargo",)


def test_the_voice_ai_feeds_spelling_is_also_read():
    """The load endpoints say `requiredClassifications`; the Voice AI feed says
    `required_carrier_classifications`. Reading only one turns a gated load into
    an ungated one."""
    record = copy.deepcopy(RECORD)
    record["reference"] = {"required_carrier_classifications": ["Critical Cargo"]}
    assert map_load(record, posted=True).required_classifications == ("Critical Cargo",)


def test_no_classifications_means_nothing_extra_is_required():
    """Null on ~90% of the live board, so this is the ordinary case."""
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["reference"]["requiredClassifications"] = None
    assert map_load(record, posted=True).required_classifications == ()
    del record["reference"]["requiredClassifications"]
    assert map_load(record, posted=True).required_classifications == ()


def test_qualifications_never_reach_what_the_carrier_is_told():
    """These gate the call; they are not facts about the freight. Reciting the
    classifications a carrier must hold tells them which answer to give, and
    reading the load's declared value invites a different conversation."""
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["reference"]["requiredClassifications"] = ["Critical Cargo"]
    record["reference"]["commodityValue"] = 250000
    spoken = map_load(record, posted=True).facts()
    assert "Critical Cargo" not in spoken
    assert "250000" not in spoken
    assert "250,000" not in spoken


def test_commodity_value_parses_and_zero_means_not_filled_in():
    record = copy.deepcopy(LOAD_DETAIL_BOOKABLE)
    record["reference"]["commodityValue"] = 250000
    assert map_load(record, posted=True).commodity_value == 250000.0
    # Strings and thousands separators both appear in this feed.
    record["reference"]["commodityValue"] = "1,250,000"
    assert map_load(record, posted=True).commodity_value == 1250000.0
    # A declared value of 0 is "nobody filled this in". Treating it as a real $0
    # would make every carrier's insurance limit look sufficient.
    for blank in (0, "0", None, False, ""):
        record["reference"]["commodityValue"] = blank
        assert map_load(record, posted=True).commodity_value is None, blank
