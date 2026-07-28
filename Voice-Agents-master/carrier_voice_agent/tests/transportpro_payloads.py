"""
Response payloads copied verbatim from the Transport Pro API collection.

`SEARCH_AVAILABLE` and `LOAD_DETAIL_WAYPOINTS` are the saved example responses
for `GET /voiceai/load/search_available` and `GET /voiceai/load/{id}`, unedited —
including the quirks the mappers exist to absorb: `false` where a value is
missing, appointment windows whose end reads before their start, and a
`carrier_sales_data` that appears both at the top level (populated) and inside
`shipment_information` (null).

`CARRIER_STATUS_*` are NOT from the collection: `GET /voiceai/carrier_status` has
no saved example response, so these are plausible shapes covering the layouts the
mapper is written to survive (flat, nested, enveloped in `results`, camelCase,
and a record with no status field at all). If the live payload turns out to look
like none of them, that is exactly the case `test_status_field_we_cannot_find`
pins down: the carrier goes to a human, not to a decline.
"""

# --- GET /voiceai/load/search_available?origin_state=IL -------------------- #
SEARCH_AVAILABLE = {
    "pagination": {
        "totalRecords": 7, "perPage": 200, "currentPage": 0, "totalPages": 1,
    },
    "results": [
        {
            "load_id": 1303369,
            "load_status": "AVAILABLE",
            "load_type": "CONTRACT",
            "shipment_information": {
                "carrier_sales_data": None,
                "sales_notes": None,
                "waypoints": [
                    {
                        "type": "Pickup",
                        "status": "Pending",
                        "city": "Nashville",
                        "state": "TN",
                        "appointment_date": {
                            "start": "2022-03-25T17:22:00Z",
                            "end": "2022-03-25T18:00:00Z",
                            "timezone": "AST",
                        },
                        "service_level": "Flexible / FCFS",
                    },
                    {
                        "type": "Delivery",
                        "city": "Marathon",
                        "state": "FL",
                        # A stop with no appointment at all: `false`, not null.
                        "appointment_date": {
                            "start": False, "end": False, "timezone": "AST",
                        },
                        "service_level": "Flexible / FCFS",
                    },
                    {
                        "type": "Final Delivery",
                        "status": "Pending",
                        "city": "Miami",
                        "state": "FL",
                        "appointment_date": {
                            "start": "2022-03-26T05:00:00Z",
                            "end": "2022-03-26T20:00:00Z",
                            "timezone": "AST",
                        },
                        "service_level": "Flexible / FCFS",
                    },
                ],
                "reference_information": [
                    {"key": "required_trailer", "value": "Van"},
                    {"key": "total_miles", "value": 1142},
                    {"key": "additional_stop_offs", "value": 0},
                    {"key": "commodity", "value": "Aircraft Metal"},
                ],
                "contacts": [
                    {
                        "title": "Customer Service Rep",
                        "name": "Gregg Brackenbury",
                        "email": "gregg.brackenbury@tenh.com",
                        "phone": "615-271-2402",
                    },
                ],
            },
            "carrier_sales_data": {
                "load_board_rate": 1600,
                "load_board_rate_per_mile": 1.4,
                "max_buy": 1800,
                "book_now_url": None,
            },
            "sales_notes": {"public_load_board_notes": "test board comment"},
            "contacts": [
                {
                    "title": "Managing Office", "code": "US",
                    "name": "Central Dispatch", "phone": "615-271-2400",
                    "email": "dispatch@tenh.com",
                },
            ],
        },
    ],
}


# The status the desk actually sells, and the only one the agent accepts by
# default. Note it is NOT what the collection's saved example above says — that
# says "AVAILABLE" — which is the whole reason the accepted set is configurable
# and the reason `test_the_collections_own_status_is_rejected_by_default` exists.
READY = "Ready To Dispatch"


def record_for(load_id, **overrides):
    """One load record as `GET /load/{id}` would serve it: sellable by default.

    `postingInfo.isPosted` is set explicitly, because that endpoint serves any
    load regardless of posting — a record with no posting flag reads as NOT on the
    board, which is what the real payloads' `postingInfo` block exists to settle.
    Pass `postingInfo=` to override it.
    """
    import copy

    record = search_available_for(load_id)["results"][0]
    record.setdefault("postingInfo", {"isPosted": True, "comments": None})
    record.update(copy.deepcopy(overrides))
    return record


def search_available_for(load_id, **overrides):
    """The example response, re-keyed to `load_id` and set Ready To Dispatch.

    The status is defaulted to the sellable one so the tests that care about
    something else — rates, lanes, the booking gate — get a load the agent will
    actually sell. Pass `load_status=` to override it and exercise the filter.
    """
    import copy

    payload = copy.deepcopy(SEARCH_AVAILABLE)
    record = payload["results"][0]
    record["load_id"] = load_id
    record["load_status"] = READY
    record.update(copy.deepcopy(overrides))
    return payload


EMPTY_SEARCH = {
    "pagination": {
        "totalRecords": 0, "perPage": 200, "currentPage": 0, "totalPages": 1,
    },
    "results": [],
}

# --- GET /voiceai/load/{id} — the collection's "Load Detail" example ------- #
# Note `shipment_information.waypoints` is empty here and the stops live under
# `dispatch_information` instead. A load in this state is not on the board.
LOAD_DETAIL_WAYPOINTS = {
    "pagination": {
        "totalRecords": 1, "perPage": 200, "currentPage": 0, "totalPages": 1,
    },
    "results": [
        {
            "load_id": 1303298,
            "load_status": "Planned",
            "dispatch_id": 649115,
            "dispatch_status": "Planned",
            "shipment_information": {
                "waypoints": [],
                "reference_information": [
                    {"key": "required_trailer", "value": "Flatbed"},
                    {"key": "total_miles", "value": 966},
                ],
                "contacts": [],
            },
            "dispatch_information": {
                "waypoints": [
                    {
                        "type": "Delivery", "status": "Pending",
                        "city": "Big Pine Key", "state": "FL",
                        "appointment_date": {
                            "start": None, "end": None, "timezone": "EST",
                        },
                    },
                    {
                        "type": "Final Delivery", "status": "Pending",
                        "city": "Slidell", "state": "LA",
                        "appointment_date": {
                            "start": "2024-12-16T18:00:00Z",
                            "end": "2024-12-17T00:00:00Z",
                            "timezone": "CST",
                        },
                    },
                ],
            },
        },
    ],
}

# --------------------------------------------------------------------------- #
# GET /load/{id} — two REAL production payloads from the Circle Logistics tenant.
#
# A different shape from the Voice AI feed in almost every respect that matters:
# stops at the top level with the city under `location`, `SH`/`CN` type codes,
# `appointmentTime.open/close` instead of `appointment_date.start/end`, rates
# under `postingInfo`, and `reference` as an OBJECT rather than a key/value list.
# They are the reason `map_load` reads each field from whichever shape carries it.
#
# The pair is also the sellability rule stated in data: identical `loadStatus`,
# opposite `isPosted`, opposite outcomes.
# --------------------------------------------------------------------------- #

# Ready To Dispatch but isPosted=false -> NOT bookable. Also carries the long
# shipper note, HTML tags and all, and a piece count of 0.
LOAD_DETAIL_UNPOSTED = {
    "id": 2520519,
    "dateCreated": "2026-07-28T18:19:22Z",
    "assignedTerminal": 1089,
    "waypoints": [
        {
            "type": "SH",
            "stopoff": False,
            "location": {
                "companyName": "JAMES RIVER LOGISTICS CENTER - ROLLED TYVEK",
                "address": "1551 Bellwood Rd",
                "address2": "BUILDING D",
                "city": "Richmond",
                "state": "VA",
                "postalCode": "23237",
                "countryCode": "USA",
                "timezone": -4,
                "ianaTimezone": "America/New_York",
            },
            "appointmentTime": {
                "open": "2026-07-29T04:00:00Z",
                "close": "2026-07-29T04:00:00Z",
                "appointmentStatus": "Not Required",
            },
            "notes": (
                "CARRIER MUST SEND BOL PRIOR TO LEAVING THE SHIPPER<br/>*Please "
                "note, we have updated our security procedures at James River "
                "Logistics Center.<br/><br/>All drivers must provide their trailer "
                "number, tractor number, trailer license plate number, tractor "
                "license plate number, state of registration for both tractor and "
                "trailer and show a valid driver’s license at the time of "
                "check in. Please make sure they are aware and prepared when they "
                "arrive.*<br/><br/>This is for all inbound and outbound shipments."
            ),
            "reference": [
                {"type": "SERVICE_LEVEL", "value": "Priority / OP8"},
                {"type": "WEIGHT", "value": 40904},
            ],
        },
        {
            "type": "CN",
            "stopoff": False,
            "location": {
                "companyName": "EIKENHOUT",
                "address": "2981 Cass Rd",
                "city": "Traverse City",
                "state": "MI",
                "postalCode": "49684",
                "timezone": -4,
                "ianaTimezone": "America/Detroit",
            },
            "appointmentTime": {
                "open": "2026-07-30T04:00:00Z",
                "close": "2026-07-30T04:00:00Z",
                "appointmentStatus": "Not Required",
            },
            "notes": None,
            "reference": [{"type": "SERVICE_LEVEL", "value": "Priority / OP8"}],
        },
    ],
    "status": {
        "loadStatus": "Ready To Dispatch",
        "documentStatus": "Waiting for Documents",
        "billingStatus": "Billing Open",
    },
    "postingInfo": {
        "isPosted": False,
        "comments": None,
        "loadBoardRate": None,
        "maxBuy": None,
    },
    "reference": {
        "billOfLading": "7805216944",
        "equipmentType": "Van",
        "miles": 861,
        "commodity": "Raw Materials Misc",
        "commodityDesc": "",
        "weight": 40904,
        "numberOfPieces": 0,
        "reeferTemperature": None,
        "poNumber": "138945",
        "hazmat": False,
        # Length alone is the trailer, not the freight.
        "dimensions": {"length": "53.00", "width": None, "height": None,
                       "volume": None},
    },
    "billingInfo": {
        "customerId": 6536,
        "customer": {"id": 6536, "companyName":
                     "DuPont Specialty Products USA LLC c/o CASS Information Systems"},
        # What the CUSTOMER pays us. Must never reach a carrier as a rate.
        "charges": {"totalFreight": 2890},
    },
}

# Ready To Dispatch AND isPosted=true -> bookable, floor 5025 / max buy 5200.
LOAD_DETAIL_BOOKABLE = {
    "id": 2520571,
    "dateCreated": "2026-07-28T18:33:19Z",
    "assignedTerminal": 1078,
    "waypoints": [
        {
            "type": "SH",
            "stopoff": False,
            "location": {
                "companyName": "C/O AMERICOLD - DC",
                "address": "2500 ROSE PARKWAY",
                "city": "SIKESTON",
                "state": "MO",
                "postalCode": "63801",
                "timezone": -5,
                "ianaTimezone": "America/Chicago",
            },
            # Converting this marker to Chicago time would move the pickup back to
            # July 29th. See `_appointment`.
            "appointmentTime": {
                "open": "2026-07-30T03:00:00Z",
                "close": "2026-07-30T03:00:00Z",
                "appointmentStatus": "Not Required",
            },
            "notes": "########################<br/>BOL # 0034850710",
            "reference": [
                {"type": "SERVICE_LEVEL", "value": "Flexible / FCFS"},
                {"type": "WEIGHT", "value": 38309},
                {"type": "PIECE_COUNT", "value": 5040},
            ],
        },
        {
            "type": "CN",
            "stopoff": False,
            "location": {
                "companyName": "GLACIERPOINT MID ATLANTIC",
                "address": "3490 N MILL ROAD",
                "city": "VINELAND",
                "state": "NJ",
                "postalCode": "08360",
                "timezone": -4,
                "ianaTimezone": "America/New_York",
            },
            "appointmentTime": {
                "open": "2026-07-31T14:00:00Z",
                "close": "2026-07-31T14:00:00Z",
                "appointmentStatus": "Not Required",
            },
            "notes": "##############################<br/>PO # 35659",
            "reference": [{"type": "SERVICE_LEVEL", "value": "Flexible / FCFS"}],
        },
    ],
    "status": {
        "loadStatus": "Ready To Dispatch",
        "documentStatus": "Waiting for Documents",
        "billingStatus": "Billing Open",
    },
    "postingInfo": {
        "isPosted": True,
        "comments": None,
        "loadBoardRate": 5025,
        "maxBuy": 5200,
    },
    "reference": {
        "billOfLading": "0034850710",
        "equipmentType": "Reefer",
        "miles": 958.58,
        "commodity": "Ice Cream",
        "commodityDesc": "ICE CREAM",
        "weight": 38309,
        "numberOfPieces": 5040,
        "reeferTemperature": "-20",
        "poNumber": "35659",
        "hazmat": False,
        "dimensions": {"length": None, "width": None, "height": None,
                       "volume": None},
    },
    "billingInfo": {
        "customerId": 6975,
        "customer": {"id": 6975, "companyName": "Unilever NASCC AG"},
        "charges": {"totalFreight": 6092, "totalFuel": 679.84},
    },
}


# --------------------------------------------------------------------------- #
# GET /voiceai/carrier_status — the REAL shape, captured from the live tenant.
#
# The collection has no saved example for this endpoint, so the mapper was
# written to find its fields by name across whatever arrived. This is what
# actually arrives:
#
#   * the carrier sits in a `carrier_record` LIST, not at the top level
#   * alongside it is `carrier_onboarding_team`, whose `phone` is at the same
#     depth as the carrier's own fields — hence the explicit unwrapping
#   * `state` is the carrier's ADDRESS ("Illinois"), never a status
#   * the vetting verdict is `ACTIVE` / `FAIL` / `REVIEW` — not the
#     active/inactive/suspended vocabulary the rest of the API uses
# --------------------------------------------------------------------------- #
def carrier_status_live(status, *, mc="123456", dot="1000001", carrier_id=13167,
                        name="Blue Sky Logistics LLC"):
    """A `carrier_status` response in the live shape, with the given verdict."""
    return {
        "carrier_record": [
            {
                "id": carrier_id,
                "status": status,
                "carrier_name": name,
                "city": "Burr Ridge",
                "state": "Illinois",        # the address, NOT a status
                "dot_number": dot,
                "mc_number": mc,
            },
        ],
        "carrier_onboarding_team": {
            "contact": None,
            "email": None,
            "phone": "260-208-4500",
        },
    }


# Verbatim from the live tenant: DOT 2999221 comes back FAIL.
CARRIER_STATUS_LIVE_FAIL = carrier_status_live(
    "FAIL", mc="23152", dot="2999221", carrier_id=18885,
    name="Creed Transport Inc")
CARRIER_STATUS_LIVE_ACTIVE = carrier_status_live("ACTIVE", mc="556949")
CARRIER_STATUS_LIVE_REVIEW = carrier_status_live("REVIEW", mc="277164")


# --- Hand-written shapes, kept so the by-name field search stays covered ----- #
CARRIER_STATUS_ACTIVE = {
    "carrier_id": 13167,
    "company_name": "Blue Sky Logistics LLC",
    "mc_number": "123456",
    "dot_number": "1000001",
    "carrier_status": "active",
    "insurance_on_file": True,
}

CARRIER_STATUS_INACTIVE = {
    "carrier_id": 13168,
    "company_name": "Dormant Transport LLC",
    "mc_number": "555444",
    "dot_number": "6000006",
    "carrier_status": "Inactive",
}

CARRIER_STATUS_SUSPENDED_ENVELOPED = {
    "pagination": {"totalRecords": 1, "perPage": 200},
    "results": [
        {
            "carrierId": 13169,
            "companyName": "Ghost Carrier LLC",
            "mcNumber": "999888",
            "dotNumber": "3000003",
            "carrierStatus": "SUSPENDED",
        },
    ],
}

# A word we have never seen. Must fail closed to suspended, never to active.
CARRIER_STATUS_UNKNOWN_WORD = {
    "carrier_id": 13170,
    "company_name": "Mystery Freight",
    "mc_number": "424242",
    "dot_number": "4242424",
    "carrier_status": "Do Not Use",
}

# No status field anywhere. Not a decline — a mapping gap, so: a human.
CARRIER_STATUS_NO_STATUS_FIELD = {
    "carrier_id": 13171,
    "company_name": "Unlabelled Carriers Inc",
    "mc_number": "313131",
    "dot_number": "3131313",
    "state": "TN",          # the address, which must never be read as a status
}

# --- GET /contact/search?connnectionRecordType=brokerCarrier --------------- #
CONTACT_SEARCH = {
    "pagination": {"totalRecords": 2, "perPage": 200},
    "results": [
        {
            "contact_id": 1000, "name": "Dispatch Desk",
            "email": "Dispatch@BlueSkyLogistics.com", "phone": "615-555-0100",
        },
        {
            "contact_id": 1001, "name": "Billing",
            "email": "billing@blueskylogistics.com", "phone": None,
        },
    ],
}
