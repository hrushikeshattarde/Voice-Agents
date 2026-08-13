"""
The office filter on both load paths, through the repository.

Two paths have to be gated, and missing either one leaks another office's freight
into a Fort Wayne call:

  get_load()    a carrier reads out a load number. If it belongs to Corporate,
                this desk cannot sell it — and must not pitch it.
  open_loads()  the alternatives read out when their number misses. Every one has
                to be this office's.

The `terminalId` filter is applied server-side AND re-checked per record, because
a search endpoint that doesn't recognise a filter tends to ignore it rather than
reject it — and an ignored filter here means the agent offers freight it can't book.
"""

import httpx
import pytest

from tests.transportpro_fake import FakeTransportPro, repository
from tests.transportpro_payloads import record_for

FW_OFFICE = 1003          # Fort Wayne Office, terminalCode "1001"
FW_POD = 1078             # POD (Carrigan Charnstrom), parented under it
FW_DORMANT = 1058         # INACTIVE POD, still Fort Wayne's
OTHER = 1068              # Tinley Park Office, under Corporate

TERMINALS = {"results": [
    {"id": 1003, "parentTerminalId": None, "terminalCode": "1001",
     "title": "Fort Wayne Office", "status": "ACTIVE"},
    {"id": 1058, "parentTerminalId": "1003", "terminalCode": "100",
     "title": "POD 1 (Ford Dedicated)", "status": "INACTIVE"},
    {"id": 1078, "parentTerminalId": "1003", "terminalCode": "113",
     "title": "POD (Carrigan Charnstrom)", "status": "ACTIVE"},
    {"id": 1015, "parentTerminalId": None, "terminalCode": "2001",
     "title": "Corporate", "status": "ACTIVE"},
    {"id": 1068, "parentTerminalId": "1015", "terminalCode": "200",
     "title": "Tinley Park Office", "status": "ACTIVE"},
]}

_FW = {"transport_pro_office_terminal_code": "1001"}


def _load(load_id: int, terminal):
    record = record_for(load_id)
    record["assignedTerminal"] = terminal
    return record


@pytest.fixture
def fake():
    server = FakeTransportPro()
    server.json("/terminal/search", TERMINALS)
    return server


def _repo(fake, audit, **overrides):
    return repository(fake, audit, **(_FW | overrides))


def _id_of(record):
    """`record_for` builds the Voice AI shape, which spells it `load_id`."""
    return record.get("id") or record.get("load_id")


def _board(fake, *records):
    """Serve /load/{id} for each record, and make /load/search terminal-aware."""
    for record in records:
        fake.json(f"/load/{_id_of(record)}", record)

    def search(request):
        wanted = request.url.params.get("terminalId")
        rows = [r for r in records
                if wanted is None or str(r.get("assignedTerminal")) == str(wanted)]
        return httpx.Response(200, json={"results": rows})

    fake.on("/load/search", search)
    return fake


# --------------------------------------------------------------------------- #
# get_load — a number the caller read out
# --------------------------------------------------------------------------- #
def test_the_offices_own_load_is_sellable(fake, repo):
    _board(fake, _load(1303369, FW_OFFICE))
    load = _repo(fake, repo).get_load("1303369")
    assert load is not None
    assert load.terminal_id == "1003"
    assert load.is_bookable


def test_a_load_on_one_of_its_pods_is_sellable(fake, repo):
    """The case that matters most: 99% of Fort Wayne's freight sits on PODs, so a
    scope that only accepted the office id would reject nearly all of it."""
    _board(fake, _load(1303369, FW_POD))
    load = _repo(fake, repo).get_load("1303369")
    assert load is not None and load.terminal_id == "1078"


def test_a_load_on_a_dormant_pod_is_still_sellable(fake, repo):
    """INACTIVE terminal, live load. Still this office's freight."""
    _board(fake, _load(1303369, FW_DORMANT))
    assert _repo(fake, repo).get_load("1303369") is not None


def test_another_offices_load_reads_as_not_on_the_board(fake, repo):
    """Dropped to None — the same answer as a load that doesn't exist — so the
    agent says it hasn't got that one and offers its own instead."""
    _board(fake, _load(1303369, OTHER))
    assert _repo(fake, repo).get_load("1303369") is None


def test_an_unreadable_terminal_is_out_of_scope_by_default(fake, repo):
    """The requirement is this office's loads only, so a load we cannot attribute
    must not be assumed ours."""
    _board(fake, _load(1303369, None))
    assert _repo(fake, repo).get_load("1303369") is None


def test_allow_unknown_terminal_opens_that_gate(fake, repo):
    """For the window where field names are being checked against live data."""
    _board(fake, _load(1303369, None))
    assert _repo(fake, repo,
                 transport_pro_allow_unknown_terminal=True
                 ).get_load("1303369") is not None


def test_with_no_office_configured_nothing_is_filtered(fake, repo):
    """The default, and the previous behaviour: the whole company board."""
    _board(fake, _load(1303369, OTHER))
    assert repository(fake, repo).get_load("1303369") is not None


def test_the_terminal_tree_is_read_once_across_many_lookups(fake, repo):
    """Org structure, not freight. Re-walking it per call would spend a round trip
    to learn something that changes when somebody is hired."""
    _board(fake, _load(1303369, FW_POD), _load(1303370, FW_OFFICE))
    tp = _repo(fake, repo)
    tp.get_load("1303369")
    tp.get_load("1303370")
    tp.open_loads()
    assert len(fake.calls("/terminal/search")) == 1


# --------------------------------------------------------------------------- #
# open_loads — the alternatives read out
# --------------------------------------------------------------------------- #
def test_open_loads_only_returns_this_offices_freight(fake, repo):
    _board(fake,
           _load(1303369, FW_OFFICE),
           _load(1303370, FW_POD),
           _load(1303371, OTHER),
           _load(1303372, OTHER))
    ids = {load.load_id for load in _repo(fake, repo).open_loads()}
    assert ids == {"1303369", "1303370"}


def test_open_loads_searches_the_office_before_its_pods(fake, repo):
    _board(fake, _load(1303369, FW_OFFICE), _load(1303370, FW_POD))
    _repo(fake, repo, transport_pro_max_offered_loads=5).open_loads()
    asked = [r.url.params.get("terminalId") for r in fake.calls("/load/search")]
    assert asked[0] == "1003"
    # Dormant PODs are not searched at all.
    assert "1058" not in asked


def test_the_search_stops_once_the_cap_is_met(fake, repo):
    """One request per terminal, so stopping early is what keeps this off the
    critical path of a live call."""
    _board(fake, _load(1303369, FW_OFFICE), _load(1303370, FW_POD))
    loads = _repo(fake, repo, transport_pro_max_offered_loads=1).open_loads()
    assert len(loads) == 1
    assert len(fake.calls("/load/search")) == 1      # never reached the POD


def test_a_filter_the_endpoint_ignored_is_caught_per_record(fake, repo):
    """If /load/search ever stops honouring terminalId, the re-check is the only
    thing standing between the agent and another office's freight."""
    records = [_load(1303369, FW_OFFICE), _load(1303371, OTHER)]
    for record in records:
        fake.json(f"/load/{_id_of(record)}", record)
    # Deliberately ignores terminalId — every request answers with both loads.
    fake.json("/load/search", {"results": records})

    loads = _repo(fake, repo).open_loads()
    assert {load.load_id for load in loads} == {"1303369"}


def test_a_load_reachable_from_two_terminals_is_not_read_out_twice(fake, repo):
    """Reading the same number to a carrier twice makes the agent sound broken."""
    record = _load(1303369, FW_OFFICE)
    fake.json(f"/load/{_id_of(record)}", record)
    fake.json("/load/search", {"results": [record]})   # same row for every terminal

    loads = _repo(fake, repo, transport_pro_max_offered_loads=5).open_loads()
    assert [load.load_id for load in loads] == ["1303369"]


def test_no_office_configured_makes_one_unfiltered_search(fake, repo):
    _board(fake, _load(1303369, OTHER))
    repository(fake, repo).open_loads()
    calls = fake.calls("/load/search")
    assert len(calls) == 1
    assert calls[0].url.params.get("terminalId") is None


# --------------------------------------------------------------------------- #
# Configuration failures
# --------------------------------------------------------------------------- #
def test_a_pinned_id_list_skips_the_tree_walk_entirely(fake, repo):
    """The escape hatch for a scope that isn't a clean subtree, or for keeping the
    filter working if /terminal/search is unavailable."""
    _board(fake, _load(1303369, FW_POD), _load(1303370, OTHER))
    tp = repository(fake, repo, transport_pro_office_terminal_code="",
                    transport_pro_office_terminal_ids="1078, 1003")

    assert tp.get_load("1303369") is not None
    assert tp.get_load("1303370") is None
    assert fake.calls("/terminal/search") == []


def test_an_unresolvable_office_code_falls_back_to_no_filtering(fake, repo):
    """Loud in the log, permissive in behaviour. The alternative — an empty scope
    read as "nothing is sellable" — is an agent that silently cannot sell at all."""
    _board(fake, _load(1303369, OTHER))
    tp = _repo(fake, repo, transport_pro_office_terminal_code="9999")
    assert tp.get_load("1303369") is not None


def test_an_unavailable_terminal_endpoint_falls_back_to_no_filtering(fake, repo):
    _board(fake, _load(1303369, OTHER))
    fake.on("/terminal/search", httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"))
    assert _repo(fake, repo).get_load("1303369") is not None


# --------------------------------------------------------------------------- #
# Pagination, through the repository
#
# `/load/search` caps a page at 200 rows. Before it was paged, a board whose first
# page held nothing sellable produced NO alternatives at all, even with hundreds of
# good loads waiting on page 2.
# --------------------------------------------------------------------------- #
def _paged_board(fake, per_page, *records):
    """Serve `records` in pages of `per_page`, honouring `page` like the real API."""
    for record in records:
        fake.json(f"/load/{_id_of(record)}", record)

    def search(request):
        wanted = request.url.params.get("terminalId")
        rows = [r for r in records
                if wanted is None or str(r.get("assignedTerminal")) == str(wanted)]
        page = int(request.url.params.get("page") or 0)
        start = page * per_page
        return httpx.Response(200, json={
            "pagination": {"totalRecords": len(rows), "perPage": per_page,
                           "currentPage": page,
                           "totalPages": max(1, -(-len(rows) // per_page))},
            "results": rows[start:start + per_page]})

    fake.on("/load/search", search)
    return fake


def _unsellable(load_id, terminal):
    """Right office, right status, but no Load Board Rate — so not quotable.

    The rates live in the TOP-LEVEL `carrier_sales_data`; the one under
    `shipment_information` is null in this payload. This is the real "Max Buy with
    no anchor" case the mapper logs about.
    """
    record = _load(load_id, terminal)
    record["carrier_sales_data"] = {"max_buy": 2000}
    return record


def test_a_sellable_load_on_a_later_page_is_still_found(fake, repo):
    """The truncation bug. Page 0 is entirely unsellable; the good load is on
    page 2 and used to be invisible."""
    records = [_unsellable(1300000 + i, FW_OFFICE) for i in range(4)]
    records.append(_load(1303369, FW_OFFICE))
    _paged_board(fake, 2, *records)

    loads = _repo(fake, repo).open_loads()
    assert [load.load_id for load in loads] == ["1303369"]
    pages = [r.url.params.get("page") for r in fake.calls("/load/search")]
    assert pages[:3] == ["0", "1", "2"]


def test_paging_stops_as_soon_as_the_cap_is_met(fake, repo):
    """Laziness matters here: this runs while a carrier holds the line."""
    records = [_load(1300000 + i, FW_OFFICE) for i in range(10)]
    _paged_board(fake, 2, *records)

    loads = _repo(fake, repo, transport_pro_max_offered_loads=3).open_loads()
    assert len(loads) == 3
    # Two pages of two, then it had enough — never asked for page 2.
    pages = [r.url.params.get("page") for r in fake.calls("/load/search")]
    assert pages == ["0", "1"]


def test_the_page_cap_is_honoured_from_settings(fake, repo):
    """Nothing sellable anywhere, so it pages until the cap rather than forever."""
    records = [_unsellable(1300000 + i, FW_OFFICE) for i in range(20)]
    _paged_board(fake, 2, *records)

    loads = _repo(fake, repo, transport_pro_max_search_pages=3,
                  transport_pro_office_terminal_ids="1003",
                  transport_pro_office_terminal_code="").open_loads()
    assert loads == []
    pages = [r.url.params.get("page") for r in fake.calls("/load/search")]
    assert pages == ["0", "1", "2"]
