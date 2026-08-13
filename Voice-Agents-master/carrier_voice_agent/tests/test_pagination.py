"""
Paging the search endpoints, and refusing to loop while a carrier is on the line.

`/load/search` caps a page at 200 rows and the live board ran to 787 over 4 pages,
so reading page 0 only silently truncated the board — the agent read out
alternatives drawn from an arbitrary slice of it.

The parameter is `page`, and that is the ONLY spelling this API honours.
`currentPage`, `pageNumber`, `pageNo`, `offset`, `start` and `skip` were each tried
against the live tenant and every one is IGNORED — the response is page 0 again.
That is the whole reason this file is mostly about guards: a paginator built on an
ignored parameter never terminates, and it never terminates DURING A PHONE CALL.

`perPage` is ignored too (50, 200 and 500 all answer 200), so page size is not
ours to choose and is read off the response instead of assumed.
"""

import httpx
import pytest

from lanevoice.integrations.transportpro.client import TransportProClient
from lanevoice.settings import get_settings

PER_PAGE = 4          # small stand-in for the real 200, same arithmetic


def _settings(**overrides):
    return get_settings().model_copy(update={
        "transport_pro_url": "https://tp.test/publicapi",
        "transport_pro_username": "u", "transport_pro_password": "p",
        **overrides})


class _Pager:
    """Serves `total` load rows in pages of `per_page`, honouring `page`."""

    def __init__(self, total, per_page=PER_PAGE, *, honour_page=True,
                 envelope=True, report_total_pages=True):
        self.total = total
        self.per_page = per_page
        self.honour_page = honour_page
        self.envelope = envelope
        self.report_total_pages = report_total_pages
        self.pages_asked: list[str | None] = []

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        asked = request.url.params.get("page")
        self.pages_asked.append(asked)
        page = int(asked) if self.honour_page and asked is not None else 0
        start = page * self.per_page
        rows = [{"id": 9000 + i, "load_id": 9000 + i}
                for i in range(start, min(start + self.per_page, self.total))]
        if not self.envelope:
            return httpx.Response(200, json={"results": rows})
        pagination = {"totalRecords": self.total, "perPage": self.per_page,
                      "currentPage": page}
        if self.report_total_pages:
            pagination["totalPages"] = -(-self.total // self.per_page)
        return httpx.Response(200, json={"pagination": pagination, "results": rows})


def _client(pager, **overrides):
    return TransportProClient(_settings(**overrides), transport=pager.transport())


def _ids(records):
    return [r["id"] for r in records]


# --------------------------------------------------------------------------- #
# Reading the whole board
# --------------------------------------------------------------------------- #
def test_every_page_is_read():
    """The bug this fixes: 11 rows over 3 pages used to come back as 4."""
    pager = _Pager(total=11)
    got = _client(pager).search_loads()
    assert len(got) == 11
    assert _ids(got) == list(range(9000, 9011))
    assert pager.pages_asked == ["0", "1", "2"]


def test_a_board_that_fits_one_page_costs_one_request():
    pager = _Pager(total=3)
    assert len(_client(pager).search_loads()) == 3
    assert pager.pages_asked == ["0"]


def test_an_exactly_full_page_checks_for_one_more():
    """`totalPages` says there is nothing after page 0, so it stops there rather
    than spending a round trip to discover an empty page."""
    pager = _Pager(total=PER_PAGE)
    assert len(_client(pager).search_loads()) == PER_PAGE
    assert pager.pages_asked == ["0"]


def test_a_full_page_with_no_total_pages_asks_again_then_stops():
    """Without `totalPages` a full page is indistinguishable from a truncated one,
    so it asks — and the empty next page ends it."""
    pager = _Pager(total=PER_PAGE, report_total_pages=False)
    assert len(_client(pager).search_loads()) == PER_PAGE
    assert pager.pages_asked == ["0", "1"]


def test_the_filters_are_carried_onto_every_page():
    """A filter dropped on page 2 would quietly widen the search — for an
    office-scoped deployment that means another office's freight from page 2 on."""
    pager = _Pager(total=9)
    pager.urls = []
    original = pager._handle

    def recording(request):
        if not request.url.path.endswith("/auth"):
            pager.urls.append(request.url)
        return original(request)

    client = TransportProClient(_settings(), transport=httpx.MockTransport(recording))
    list(client.iter_search_loads(is_posted=True, terminal_id=1003,
                                  load_status="ready to dispatch"))

    assert len(pager.urls) == 3
    for url in pager.urls:
        assert url.params.get("terminalId") == "1003"
        assert url.params.get("isPosted") == "true"
        assert url.params.get("loadStatus") == "ready to dispatch"
    assert [u.params.get("page") for u in pager.urls] == ["0", "1", "2"]


def test_a_page_is_requested_explicitly_even_for_the_first():
    """`page=0` is sent rather than omitted, so the request log shows what was
    asked for and a missing-parameter bug can't hide behind a default."""
    pager = _Pager(total=2)
    _client(pager).search_loads()
    assert pager.pages_asked == ["0"]


# --------------------------------------------------------------------------- #
# The iterator is lazy — this is what keeps it off the critical path
# --------------------------------------------------------------------------- #
def test_stopping_early_never_fetches_the_later_pages():
    """`open_loads` wants five loads, not 800. Laziness is the whole reason
    `iter_search_loads` exists alongside `search_loads`."""
    pager = _Pager(total=100)
    client = _client(pager)

    taken = []
    for record in client.iter_search_loads():
        taken.append(record)
        if len(taken) == 2:
            break

    assert len(taken) == 2
    assert pager.pages_asked == ["0"]        # 25 pages available, 1 fetched


def test_search_loads_is_the_eager_form():
    pager = _Pager(total=10)
    assert len(_client(pager).search_loads()) == 10
    assert len(pager.pages_asked) == 3


# --------------------------------------------------------------------------- #
# Guards. Each one prevents a loop that would happen mid-call.
# --------------------------------------------------------------------------- #
def test_an_ignored_page_parameter_stops_instead_of_looping():
    """The live failure mode. Six of the seven plausible parameter names are
    silently ignored by this API — without this guard, one of them would spin
    forever yielding the same 200 rows."""
    pager = _Pager(total=100, honour_page=False)
    got = _client(pager).search_loads()

    # Page 0's rows, once, and then it gave up.
    assert _ids(got) == list(range(9000, 9000 + PER_PAGE))
    assert pager.pages_asked == ["0", "1"]


def test_a_response_reporting_the_wrong_page_stops():
    """A server that answers page 3 with `currentPage: 0` is not paginating, even
    if the rows happen to differ."""
    class _Liar(_Pager):
        def _handle(self, request):
            response = super()._handle(request)
            if request.url.path.endswith("/auth"):
                return response
            body = response.json()
            body["pagination"]["currentPage"] = 0      # always claims page 0
            return httpx.Response(200, json=body)

    pager = _Liar(total=100)
    got = _client(pager).search_loads()
    assert len(got) == PER_PAGE
    assert pager.pages_asked == ["0", "1"]


def test_duplicate_rows_across_pages_are_yielded_once():
    """The board MOVES while it is read — `totalRecords` was seen dropping from
    826 to 787 between two requests — so a record genuinely can straddle a page
    boundary. Reading the same load number out twice makes the agent sound broken."""
    class _Overlapping(_Pager):
        def _handle(self, request):
            if request.url.path.endswith("/auth"):
                return super()._handle(request)
            page = int(request.url.params.get("page") or 0)
            self.pages_asked.append(str(page))
            # Every page repeats the previous page's last row.
            start = max(0, page * self.per_page - page)
            rows = [{"id": 9000 + i, "load_id": 9000 + i}
                    for i in range(start, min(start + self.per_page, self.total))]
            return httpx.Response(200, json={
                "pagination": {"totalRecords": self.total, "perPage": self.per_page,
                               "currentPage": page,
                               "totalPages": -(-self.total // self.per_page)},
                "results": rows})

    got = _client(_Overlapping(total=12)).search_loads()
    assert len(_ids(got)) == len(set(_ids(got)))


def test_an_unpaginated_response_costs_exactly_one_request():
    """A bare `{"results": [...]}` with no envelope is not paginated. Asking for
    page 1 could only ever return the same rows."""
    pager = _Pager(total=PER_PAGE, envelope=False)
    assert len(_client(pager).search_loads()) == PER_PAGE
    assert pager.pages_asked == ["0"]


def test_an_empty_first_page_yields_nothing():
    pager = _Pager(total=0)
    assert _client(pager).search_loads() == []
    assert pager.pages_asked == ["0"]


def test_the_page_cap_bounds_a_runaway():
    """Backstop for a board that never ends. Truncation is logged as an ERROR,
    because silently reading 2000 of 5000 loads is the bug this file fixes."""
    pager = _Pager(total=1000)
    got = list(_client(pager).iter_search_loads(max_pages=3))
    assert len(got) == 3 * PER_PAGE
    assert pager.pages_asked == ["0", "1", "2"]


def test_the_default_cap_is_generous_but_finite():
    """10 pages = 2000 loads at the real page size. A runaway backstop, not a
    target: the board search stops as soon as it has enough loads to read out, so
    a second page is only fetched when the first was full of unsellable ones.

    Read off the field default so a developer's `.env` can't fail the suite. The
    repository threading this into the client is covered by the office-scope
    tests, which drive `open_loads` end to end.
    """
    from lanevoice.settings import Settings

    assert Settings.model_fields["transport_pro_max_search_pages"].default == 10


# --------------------------------------------------------------------------- #
# The terminal tree pages too
# --------------------------------------------------------------------------- #
def test_the_terminal_tree_is_paged():
    """172 rows fits one page today. An org crossing 200 terminals would otherwise
    lose its deepest PODs from every office's scope — silently, and only for the
    offices at the end of the list."""
    class _Terminals(_Pager):
        def _handle(self, request):
            if request.url.path.endswith("/auth"):
                return super()._handle(request)
            page = int(request.url.params.get("page") or 0)
            self.pages_asked.append(str(page))
            start = page * self.per_page
            rows = [{"id": 1000 + i, "parentTerminalId": None,
                     "terminalCode": str(i), "title": f"T{i}", "status": "ACTIVE"}
                    for i in range(start, min(start + self.per_page, self.total))]
            return httpx.Response(200, json={
                "pagination": {"totalRecords": self.total, "perPage": self.per_page,
                               "currentPage": page,
                               "totalPages": -(-self.total // self.per_page)},
                "results": rows})

    pager = _Terminals(total=10)
    rows = _client(pager).terminal_search()
    assert len(rows) == 10
    assert pager.pages_asked == ["0", "1", "2"]


@pytest.mark.parametrize("total", [0, 1, PER_PAGE - 1, PER_PAGE, PER_PAGE + 1,
                                   PER_PAGE * 3, PER_PAGE * 3 + 1])
def test_every_boundary_returns_exactly_the_right_count(total):
    """Off-by-one at a page boundary either drops a load or repeats one."""
    got = _client(_Pager(total=total)).search_loads()
    assert len(got) == total
    assert _ids(got) == list(range(9000, 9000 + total))
