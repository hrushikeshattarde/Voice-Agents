"""
Which office's freight is this? — the terminal scope.

Transport Pro hangs every load off a terminal (`assignedTerminal`), and terminals
form a tree: an OFFICE at the root with PODs and teams parented under it. A
deployment that serves one office may only quote and book that office's loads, so
the scope is the office terminal PLUS its whole subtree.

Getting the subtree right is not optional, and the numbers say why. On the live
tenant, Fort Wayne Office is `id=1003` and carries **4** posted loads of its own,
while its 49 PODs carry another **338** — POD (Carrigan Charnstrom) alone has 80
and POD (Frankie Saiz) 79. Scoping to the office id would therefore hide 99% of
the office's own freight while looking like it worked.

Three traps in this data, all of them silent:

* **`parentTerminalId` is a STRING; `id` is an INT.** Verified across all 172
  rows: `{"id": 1058, "parentTerminalId": "1003"}`. An equality-based walk finds
  no children at all and every office looks childless — which is exactly the "4
  loads" result above. Everything here is keyed on normalised strings.

* **`terminalCode` is not `id`.** The office is code `"1001"` and id `1003`; its
  PODs run codes 100–331. The root is resolved BY CODE because a code is what a
  human can look up and confirm, and ids are not guaranteed stable — but the walk
  and the load comparison are both on `id`, because that is what a load carries.

* **INACTIVE terminals still hold loads.** 15 of the 50 are inactive, and a load
  parked on one is still this office's load. So the SCOPE includes them (a gate
  must not reject our own freight) while the board SEARCH skips them (no point
  spending a round trip per dormant POD).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from lanevoice.logging_config import get_logger

logger = get_logger(__name__)


def _key(value: object) -> str | None:
    """A terminal identifier as a comparable string, or None.

    The one function standing between this module and the int/str mismatch above.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class TerminalScope:
    """The terminals a deployment may sell out of.

    `ids` is every terminal in the subtree, for GATING a load. `searchable` is the
    ACTIVE subset, for iterating the board. Empty `ids` means "no scope
    configured" — which callers must read as "no filtering", never as "nothing is
    in scope", or a missing setting would silently stop the agent selling anything.
    """

    ids: frozenset[str] = frozenset()
    searchable: tuple[str, ...] = ()
    titles: dict[str, str] = field(default_factory=dict)
    root_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.ids)

    def contains(self, terminal_id: object) -> bool:
        return (key := _key(terminal_id)) is not None and key in self.ids

    def title(self, terminal_id: object) -> str:
        key = _key(terminal_id)
        return self.titles.get(key or "", "") or f"terminal {terminal_id}"


def build_scope(rows: list[dict], *, root_code: str,
                extra_ids: frozenset[str] = frozenset()) -> TerminalScope:
    """Walk `/terminal/search` rows into the subtree under `root_code`.

    Returns an empty scope when the root code matches nothing — the caller logs
    that as a configuration error rather than proceeding, because an empty scope
    and "no filtering" are the same value and must not be confused.
    """
    by_id: dict[str, dict] = {}
    children: dict[str | None, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (rid := _key(row.get("id"))) is None:
            continue
        by_id[rid] = row
        children.setdefault(_key(row.get("parentTerminalId")), []).append(rid)

    wanted = str(root_code).strip()
    root = next((rid for rid, row in by_id.items()
                 if str(row.get("terminalCode") or "").strip() == wanted), None)
    if root is None:
        logger.error(
            "No terminal has terminalCode %r among the %d Transport Pro returned. "
            "TRANSPORT_PRO_OFFICE_TERMINAL_CODE is wrong, or this API user cannot "
            "see that office.", wanted, len(by_id))
        return TerminalScope()

    # Iterative, with a seen-set. The tree is shallow but a cycle in bad data
    # would hang a recursive walk, and this runs while a carrier is on the line.
    ids: set[str] = set()
    searchable: list[str] = []
    titles: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node in ids or node not in by_id:
            continue
        row = by_id[node]
        ids.add(node)
        titles[node] = str(row.get("title") or "").strip()
        if str(row.get("status") or "").strip().upper() == "ACTIVE":
            searchable.append(node)
        stack.extend(children.get(node, []))

    if extra_ids:
        # Testing escape hatch: pull in a load parked outside the office tree.
        ids |= set(extra_ids)
        searchable.extend(t for t in extra_ids if t not in searchable)
        logger.warning(
            "TRANSPORT_PRO_EXTRA_TERMINAL_IDS is set — also treating %s as in "
            "scope. Unset this in production.", sorted(extra_ids))

    # Root first, then numerically: the office's own loads are the ones a rep
    # would reach for, and a stable order keeps the spoken alternatives stable.
    searchable.sort(key=lambda t: (t != root, int(t) if t.isdigit() else 0, t))
    logger.info("Terminal scope: %s (code %s) + %d under it; %d active to search.",
                titles.get(root) or root, wanted, len(ids) - 1, len(searchable))
    return TerminalScope(ids=frozenset(ids), searchable=tuple(searchable),
                         titles=titles, root_id=root)


class TerminalScopeCache:
    """Resolves the scope once and holds it — the tree changes very rarely.

    Long TTL on purpose: this is org structure, not freight. Re-walking it per
    call would spend a round trip to learn something that changes when somebody
    is hired.
    """

    def __init__(self, ttl: float = 3600.0):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._scope: TerminalScope | None = None
        self._expires_at = 0.0

    def get(self, resolve) -> TerminalScope:
        """`resolve()` is called only on a miss; its failures are not cached."""
        with self._lock:
            if self._scope is not None and time.monotonic() < self._expires_at:
                return self._scope
        scope = resolve()
        with self._lock:
            # A failed resolve returns an unconfigured scope. Don't cache that for
            # an hour — a transient outage would disable the filter for the rest
            # of the worker's life.
            if scope.configured:
                self._scope = scope
                self._expires_at = time.monotonic() + self._ttl
        return scope

    def clear(self) -> None:
        with self._lock:
            self._scope = None
            self._expires_at = 0.0
