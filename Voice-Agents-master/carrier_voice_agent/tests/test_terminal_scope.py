"""
Office scope: which terminals' freight may this deployment sell?

The whole file exists because of one measurement on the live tenant. Fort Wayne
Office (`id=1003`, `terminalCode="1001"`) carries **4** posted loads; the 49 PODs
parented under it carry another **338**. A scope that stops at the office id looks
like it works — it returns loads, they're all Fort Wayne, nothing errors — while
hiding 99% of the office's own freight.

The bug that produces exactly that result is a type mismatch: `id` is an int and
`parentTerminalId` is a STRING, verified across all 172 rows the live endpoint
returns. An equality-based walk finds no children and every office looks
childless. `test_the_parent_link_is_a_string_and_the_id_an_int` is the guard.

The rows below are shortened copies of real ones.
"""

import pytest

from lanevoice.integrations.transportpro.terminals import (
    TerminalScope,
    TerminalScopeCache,
    build_scope,
)

# Real shapes. Note `id` int vs `parentTerminalId` str — that is the wire format.
ROWS = [
    {"id": 1003, "parentTerminalId": None, "terminalCode": "1001",
     "title": "Fort Wayne Office", "status": "ACTIVE"},
    {"id": 1058, "parentTerminalId": "1003", "terminalCode": "100",
     "title": "POD 1 (Ford Dedicated)", "status": "INACTIVE"},
    {"id": 1060, "parentTerminalId": "1003", "terminalCode": "103",
     "title": "POD (Jonathan Kiburz)", "status": "ACTIVE"},
    {"id": 1078, "parentTerminalId": "1003", "terminalCode": "113",
     "title": "POD (Carrigan Charnstrom)", "status": "ACTIVE"},
    # A grandchild: a team under a POD, to prove the walk is not one level deep.
    {"id": 1200, "parentTerminalId": "1078", "terminalCode": "400",
     "title": "Team under a POD", "status": "ACTIVE"},
    # Another office entirely, with its own children.
    {"id": 1015, "parentTerminalId": None, "terminalCode": "2001",
     "title": "Corporate", "status": "ACTIVE"},
    {"id": 1068, "parentTerminalId": "1015", "terminalCode": "200",
     "title": "Tinley Park Office", "status": "ACTIVE"},
    {"id": 1070, "parentTerminalId": "1015", "terminalCode": "201",
     "title": "Indianapolis Office", "status": "ACTIVE"},
]

FW = {"1003", "1058", "1060", "1078", "1200"}


def _scope(**kwargs):
    return build_scope(ROWS, root_code="1001", **kwargs)


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #
def test_the_scope_is_the_office_plus_its_whole_subtree():
    """Not just the office. On the live tenant that difference is 4 loads vs 342."""
    assert set(_scope().ids) == FW


def test_the_parent_link_is_a_string_and_the_id_an_int():
    """The trap this module is built around. If this ever passes trivially because
    the API started typing them the same way, the walk still works — but a walk
    that compares them RAW would find nothing, so the guard stays."""
    root = next(r for r in ROWS if r["id"] == 1003)
    child = next(r for r in ROWS if r["id"] == 1058)
    assert isinstance(root["id"], int)
    assert isinstance(child["parentTerminalId"], str)
    assert child["parentTerminalId"] != root["id"]        # 1003 != "1003"
    # And yet the child is in scope, because the walk normalises.
    assert _scope().contains(1058)


def test_a_grandchild_is_in_scope():
    """Teams sit under PODs, which sit under the office."""
    assert _scope().contains("1200")


def test_another_offices_terminals_are_out_of_scope():
    scope = _scope()
    for other in (1015, 1068, 1070):
        assert not scope.contains(other), other


def test_the_root_is_found_by_code_not_by_id():
    """Codes are what a human looks up in Transport Pro; ids are not guaranteed
    stable. The office is code "1001" and id 1003 — easy to transpose."""
    assert _scope().root_id == "1003"
    assert build_scope(ROWS, root_code="2001").root_id == "1015"


def test_an_unknown_root_code_yields_an_unconfigured_scope():
    """A typo'd code must not resolve to "everything" OR quietly to "nothing"
    without saying so — the caller reads `configured` and logs."""
    scope = build_scope(ROWS, root_code="9999")
    assert scope.configured is False
    assert scope.ids == frozenset()


def test_membership_accepts_either_type():
    """A load's `assignedTerminal` arrives as an int from one endpoint and a str
    from another."""
    scope = _scope()
    assert scope.contains(1078) and scope.contains("1078")
    assert scope.contains(" 1078 ")          # and survives stray whitespace
    assert not scope.contains(None)
    assert not scope.contains("")


# --------------------------------------------------------------------------- #
# ACTIVE vs INACTIVE — strict gate, efficient search
# --------------------------------------------------------------------------- #
def test_inactive_terminals_are_in_scope_but_not_searched():
    """A load parked on a dormant POD is still this office's load, so the GATE
    must accept it. But spending a round trip per dormant POD to look for board
    loads is waste, so the SEARCH skips them."""
    scope = _scope()
    assert scope.contains(1058)                    # INACTIVE, still ours
    assert "1058" not in scope.searchable
    assert set(scope.searchable) == {"1003", "1060", "1078", "1200"}


def test_the_office_itself_is_searched_first():
    """Its own loads are the ones a rep reaches for, and a stable order keeps the
    spoken alternatives stable between calls."""
    assert _scope().searchable[0] == "1003"


# --------------------------------------------------------------------------- #
# Escape hatches
# --------------------------------------------------------------------------- #
def test_extra_ids_are_added_to_the_subtree():
    """For testing against a load parked outside the tree, which would otherwise
    be correctly rejected."""
    scope = _scope(extra_ids=frozenset({"1145"}))
    assert scope.contains("1145")
    assert set(scope.ids) == FW | {"1145"}
    assert "1145" in scope.searchable


def test_an_unconfigured_scope_means_no_filtering():
    """The default. `contains` is irrelevant here — callers check `configured`
    first, and an empty scope must never be read as "nothing is sellable"."""
    scope = TerminalScope()
    assert scope.configured is False
    assert scope.searchable == ()


def test_titles_are_available_for_logging():
    scope = _scope()
    assert scope.title(1003) == "Fort Wayne Office"
    assert scope.title("1078") == "POD (Carrigan Charnstrom)"
    # Unknown ids still render something a human can read in a log line.
    assert "9999" in scope.title(9999)


# --------------------------------------------------------------------------- #
# Bad data must not hang a live call
# --------------------------------------------------------------------------- #
def test_a_cycle_in_the_tree_terminates():
    """A recursive walk would blow the stack; this runs while a carrier holds."""
    rows = [
        {"id": 1, "parentTerminalId": None, "terminalCode": "R", "title": "root",
         "status": "ACTIVE"},
        {"id": 2, "parentTerminalId": "1", "terminalCode": "A", "title": "a",
         "status": "ACTIVE"},
        {"id": 3, "parentTerminalId": "2", "terminalCode": "B", "title": "b",
         "status": "ACTIVE"},
        {"id": 1, "parentTerminalId": "3", "terminalCode": "R", "title": "root",
         "status": "ACTIVE"},          # 1 -> 2 -> 3 -> 1
    ]
    assert set(build_scope(rows, root_code="R").ids) == {"1", "2", "3"}


def test_rows_with_no_id_or_junk_rows_are_ignored():
    rows = [*ROWS, None, "nonsense", {}, {"parentTerminalId": "1003"}]
    assert set(build_scope(rows, root_code="1001").ids) == FW


def test_an_empty_row_set_is_unconfigured():
    assert build_scope([], root_code="1001").configured is False


# --------------------------------------------------------------------------- #
# Caching — org structure, not freight
# --------------------------------------------------------------------------- #
def test_the_scope_is_resolved_once():
    calls = []

    def resolve():
        calls.append(1)
        return _scope()

    cache = TerminalScopeCache(ttl=3600)
    assert cache.get(resolve).contains(1078)
    assert cache.get(resolve).contains(1078)
    assert len(calls) == 1


def test_a_failed_resolve_is_not_cached():
    """A transient outage must not disable the office filter for the life of the
    worker — the next call retries."""
    calls = []

    def failing():
        calls.append(1)
        return TerminalScope()

    cache = TerminalScopeCache(ttl=3600)
    assert cache.get(failing).configured is False
    assert cache.get(failing).configured is False
    assert len(calls) == 2

    # And once it succeeds, THAT is cached.
    assert cache.get(_scope).contains(1078)
    assert cache.get(lambda: pytest.fail("should not resolve again")).contains(1078)
