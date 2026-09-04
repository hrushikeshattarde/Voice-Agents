"""
`TransportProRepository` — the same repository interface, backed by the API.

The conversation and service layers depend on `Repository`'s method signatures
and on domain models, never on SQL, so pointing the agent at the real system of
record means implementing those methods against Transport Pro. Nothing in
`CarrierSalesAgent`, `NegotiationEngine` or `CarrierVerificationService` changes
shape because of this file.

Reads come from Transport Pro. The **call audit trail** — who called, what was
offered in which round, why a call was handed over — stays in the local SQLite
database, because Transport Pro has no endpoint for it and losing that record
would make a disputed booking unauditable. The things Transport Pro *does* own
are written back to it: the agreed rate as an offer, the empty call as carrier
capacity, and a note on the load.

Two deliberate degradations, both visible rather than silent:

* `carriers_matching_digits` returns nothing. It backs the "we heard four of six
  digits, let me confirm you by company name" recovery, and the API has no
  prefix search to build that on. The agent's other recovery path still works:
  `digit_readings` proposes complete candidate numbers and each is looked up, so
  a caller who was cut off or started over is still found. A caller we only ever
  hear a fragment from is asked again and then handed to a rep.

* Reps are local. They are warm-transfer targets (a name and a phone), not
  Transport Pro records, so `available_rep` reads the same seeded table it
  always did.
"""

from __future__ import annotations

import dataclasses
import datetime
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from lanevoice.db.repository import Repository
from lanevoice.domain.errors import LoadOutOfScope, SourceUnavailable
from lanevoice.domain.models import Carrier, Load, LoadStatus, OfferParty, Rep
from lanevoice.integrations.highway import mappers as highway_mappers
from lanevoice.integrations.highway.client import HighwayClient, HighwayError
from lanevoice.integrations.transportpro.client import (
    TransportProClient,
    TransportProError,
)
from lanevoice.integrations.transportpro.happyrobot import (
    HappyRobotClient,
    HappyRobotError,
)
from lanevoice.integrations.transportpro.mappers import (
    contact_emails,
    map_carrier,
    map_load,
    map_rep,
)
from lanevoice.integrations.transportpro.terminals import (
    TerminalScope,
    TerminalScopeCache,
    build_scope,
)
from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings

logger = get_logger(__name__)


class _TTLCache:
    """Tiny per-process cache with an expiry, safe for concurrent calls.

    One repository instance is shared by every call the worker handles, so
    without an expiry a load booked by somebody else would keep reading as open
    for the life of the process. Negative results are cached too: a caller
    reading out a wrong number should not cost a fresh round trip per attempt.
    """

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> tuple[bool, Any]:
        """`(hit, value)` — `hit` is False for a miss, so `None` can be cached."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                del self._entries[key]
                return False, None
            return True, value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


@dataclasses.dataclass(frozen=True)
class BookingAttempt:
    """What actually happened when we tried to produce a booking link.

    Two fields rather than one, because "no link" has two meanings that need
    different things from whoever reads the call afterwards:

        offer_id set, url None   the rate IS on the load; a rep finishes it by
                                 hand, and must NOT create a second offer
        both None                nothing was recorded; the lane is untouched

    Collapsing those into a bare `None` is how a disputed booking ends up with two
    offers against it, so the caller gets to tell them apart.
    """

    url: str | None = None
    offer_id: str | None = None

    @property
    def link_issued(self) -> bool:
        return bool(self.url)

    @property
    def offer_recorded(self) -> bool:
        return bool(self.offer_id)


# What an enrichment lookup returns when the call itself failed, as opposed to
# succeeding with "no such carrier" (None). The two are handled differently:
# a miss on the MC is followed by a try on the DOT, a failure is not.
_LOOKUP_FAILED: Any = object()


@dataclasses.dataclass(frozen=True)
class _Speculative:
    """Enrichment lookups started on the number AS HEARD, before Transport Pro
    has said whose number it is. Valid only if the record confirms that number is
    the carrier's MC — see `TransportProRepository._enrich_carrier`."""

    keyed_on: str
    highway: Future | None
    happyrobot: Future | None


class TransportProRepository:
    """Loads and carriers from Transport Pro; call audit trail in SQLite.

    `audit` is a plain `Repository` over the local database. It is required: the
    agent logs every offer, note and handoff through it, and those writes are
    what make a call reviewable afterwards.
    """

    def __init__(
        self,
        client: TransportProClient,
        audit: Repository,
        settings: Settings,
        *,
        highway: HighwayClient | None = None,
        happyrobot: HappyRobotClient | None = None,
    ):
        self._client = client
        self._audit = audit
        self._settings = settings
        # Both optional. Absent = that capability is simply off, and the agent
        # behaves exactly as it did before it existed: no Highway means vetting
        # runs on Transport Pro's list alone, no HappyRobot means a booking logs
        # the rate without producing a link.
        self._highway = highway
        self._happyrobot = happyrobot
        self._loads = _TTLCache(settings.transport_pro_load_cache_seconds)
        self._carriers = _TTLCache(settings.transport_pro_carrier_cache_seconds)
        self._emails = _TTLCache(settings.transport_pro_carrier_cache_seconds)
        # Reps are org structure — a name and a desk phone change when somebody is
        # hired — so they are held as long as the terminal tree is.
        self._reps = _TTLCache(settings.transport_pro_terminal_cache_seconds)
        # usdot (as the rest of the system keys carriers) -> Transport Pro id,
        # so `carrier_emails` can reach `/contact/search` from a USDOT alone.
        self._carrier_ids: dict[str, str] = {}
        # The Highway and HappyRobot reads that enrich a carrier run on these
        # threads, in parallel with each other and with the Transport Pro probe
        # itself — see `get_carrier`. Both clients are httpx.Clients, which are
        # safe to share across threads. Sized for several concurrent calls.
        self._lookups = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tp-enrich")
        # Which office's freight this deployment may sell. Resolved lazily on the
        # first load lookup and then held for an hour — it is org structure.
        self._terminals = TerminalScopeCache(
            settings.transport_pro_terminal_cache_seconds)

    # -- office scope ------------------------------------------------------- #
    def _scope(self) -> TerminalScope:
        """The terminals this deployment may sell out of.

        An UNCONFIGURED scope means "no filtering" and is the default. A configured
        one that resolves to nothing is a configuration error, logged loudly by
        `build_scope` — and it still reads as unconfigured here, which is the safe
        direction: the alternative is an agent that silently cannot sell anything.
        """
        if not self._settings.scopes_by_office:
            return TerminalScope()

        pinned = self._settings.office_terminal_ids
        if pinned:
            # Manual pin: no walk, no round trip. Every pinned terminal is
            # searchable, since we have no status information without the tree.
            return TerminalScope(ids=frozenset(pinned),
                                 searchable=tuple(sorted(pinned)))

        def resolve() -> TerminalScope:
            try:
                rows = self._client.terminal_search(
                    max_pages=self._settings.transport_pro_max_search_pages)
            except TransportProError as exc:
                logger.error(
                    "Could not read the terminal tree (%s), so this office's scope "
                    "is unknown. Falling back to the WHOLE company board — set "
                    "TRANSPORT_PRO_OFFICE_TERMINAL_IDS to pin the scope instead of "
                    "depending on this endpoint.", exc)
                return TerminalScope()
            return build_scope(
                rows,
                root_code=self._settings.transport_pro_office_terminal_code.strip(),
                extra_ids=self._settings.extra_terminal_ids,
            )

        return self._terminals.get(resolve)

    def _in_scope(self, load: Load, scope: TerminalScope) -> bool:
        """May this deployment sell this load?

        A load with no readable terminal is OUT of scope by default: the
        requirement is "this office's loads only", and an unreadable one must not
        be assumed to be ours. `TRANSPORT_PRO_ALLOW_UNKNOWN_TERMINAL` inverts that
        for the window where field names are being verified against live data.
        """
        if not scope.configured:
            return True
        if load.terminal_id is None:
            allowed = self._settings.transport_pro_allow_unknown_terminal
            logger.warning(
                "Load %s carries no assignedTerminal, so which office owns it "
                "cannot be established — treating it as %s "
                "(TRANSPORT_PRO_ALLOW_UNKNOWN_TERMINAL=%s).",
                load.load_id, "IN scope" if allowed else "OUT of scope", allowed)
            return allowed
        return scope.contains(load.terminal_id)

    # -- loads -------------------------------------------------------------- #
    def get_load(self, load_id: str) -> Load | None:
        """The load behind a number the caller read out.

        One call to `GET /load/{id}`, because that payload answers everything
        the call needs at once: whether the load exists, its `status.loadStatus`,
        whether `postingInfo.isPosted` is on, and the rates to open and stop at.
        A second round trip while a carrier holds the line buys nothing.

        `posted=False` is only the fallback for a payload that carries no posting
        flag at all. This endpoint serves any load regardless of posting, so "we
        could not tell" has to mean "not on the board" here.
        """
        key = _digits(load_id) or str(load_id).upper()
        hit, cached = self._loads.get(key)
        if hit:
            return cached

        try:
            record = self._client.load_detail(key)
        except TransportProError as exc:
            raise SourceUnavailable(f"load lookup for {load_id} failed: {exc}") from exc

        load = None
        if record is not None:
            if _match([record], key) is None:
                # Asked for one load and got another. Pitching it would be worse
                # than saying we can't find theirs.
                logger.error(
                    "Asked Transport Pro for load %s and got %r back — ignoring it.",
                    key, record.get("id") or record.get("load_id"))
            else:
                load = self._map(record, posted=False)

        # Another office's freight is not ours to sell — and it is NOT the same
        # answer as a load that doesn't exist. A miss invites the caller to try
        # another number; another office's load is decided, and the agent should
        # thank them and wrap up. Raised as a typed error so the conversation
        # layer can say the right thing (observed live: the old collapse into
        # "not on the board" sent a caller re-reading a posting for a number
        # that could never have worked on this desk).
        #
        # Only a KNOWN foreign terminal raises. A load whose terminal cannot be
        # read stays plain not-found: "a different desk handles that one" is a
        # claim, and it must not be made about freight we cannot attribute.
        rehearsal = load is not None and key in self._settings.test_load_ids
        if rehearsal:
            # A dummy load the desk uses to walk through the whole call. Whatever
            # Transport Pro says about its terminal, posting or status, it is
            # this desk's, posted and open; its rates and requirements are still
            # its own. Loud on purpose — this must never be on for a real board.
            logger.warning(
                "TEST LOAD %s: treating it as this desk's, posted and open "
                "(TEST_LOAD_IDS) — rehearsal only; Transport Pro has status=%s, "
                "posted=%s, terminal=%s.", load.load_id, load.status.value,
                load.is_posted, load.terminal_id)
            load = dataclasses.replace(load, status=LoadStatus.OPEN, is_posted=True)
        if load is not None and not rehearsal:
            scope = self._scope()
            if not self._in_scope(load, scope):
                if load.terminal_id is not None:
                    logger.info(
                        "Load %s belongs to %s, which is outside this deployment's "
                        "office scope (%s). Closing rather than retrying.",
                        load.load_id, scope.title(load.terminal_id),
                        scope.title(scope.root_id))
                    raise LoadOutOfScope(
                        f"load {load.load_id} belongs to "
                        f"{scope.title(load.terminal_id)}, outside this desk's scope")
                load = None

        if load is not None and not load.is_bookable:
            logger.info(
                "Transport Pro load %s is not sellable: status=%s, posted=%s. "
                "The agent will not offer it.",
                load.load_id, load.status.value, load.is_posted)
        if load is not None and not load.is_quotable and load.is_bookable:
            logger.error(
                "Transport Pro load %s is posted and open but has no usable Load "
                "Board Rate (open=%s, max_buy=%s) — the agent has no anchor to "
                "open at and will hand the call to a rep.",
                load.load_id, load.open_rate, load.ceiling_rate)
        self._loads.put(key, load)
        return load

    def _map(self, record: dict, *, posted: bool) -> Load | None:
        """Map a load record under this deployment's sellability rules."""
        return map_load(
            record,
            posted=posted,
            fraud_low_ratio=self._settings.transport_pro_fraud_low_ratio,
            open_statuses=self._settings.open_load_statuses,
        )

    def open_loads(self) -> list[Load]:
        """Loads the agent may actually sell — capped, because these are spoken.

        Every one of these has cleared both conditions (a sellable status and
        posting switched on) and has a rate to open at. `_identify_load` reads
        them out by number when a caller's own number doesn't check out, so
        anything unsellable in here becomes a load we offered and then refused.
        Reading two hundred numbers down a phone line is not a thing a rep does,
        hence the cap — applied to the loads that PASSED, so a board full of
        unsellable ones doesn't crowd out the good ones.

        When the deployment is scoped to one office this walks that office's
        terminals, one request each, and stops as soon as the cap is met.
        `terminalId` matches a single terminal exactly — it does not descend the
        tree — so there is no one-request version of this. The office's own
        terminal is tried first, then its PODs, and in practice the cap is reached
        within the first request or two.
        """
        hit, cached = self._loads.get("__open__")
        if hit:
            return cached

        # `/load/search` wants a pickup window, and an unbounded one would drag
        # back the whole board to read out five numbers. A carrier calling today
        # is looking for freight this week.
        today = datetime.date.today()
        horizon = today + datetime.timedelta(
            days=max(0, self._settings.transport_pro_open_load_days))
        # The desk's two conditions, applied server-side this time — and still
        # re-checked on every record by `_map`.
        wanted = sorted(self._settings.open_load_statuses)
        status = wanted[0] if len(wanted) == 1 else None
        cap = self._settings.transport_pro_max_offered_loads

        scope = self._scope()
        # `[None]` = one unfiltered request, the whole company board. That request
        # sees only the first 200 rows of a board that runs to 826, which is
        # another reason an office-scoped deployment is the better configuration.
        targets = scope.searchable if scope.configured else (None,)

        loads: list[Load] = []
        seen, skipped, searched = set(), 0, 0
        try:
            for terminal in targets:
                if len(loads) >= cap:
                    break
                searched += 1
                # An ITERATOR, so the board's later pages are fetched only if the
                # earlier ones didn't yield enough sellable loads. The unfiltered
                # board runs to ~800 over 4 pages; the agent needs five.
                records = self._client.iter_search_loads(
                    pickup_date_start=today.isoformat(),
                    pickup_date_end=horizon.isoformat(),
                    load_status=status,
                    is_posted=True,
                    terminal_id=terminal,
                    max_pages=self._settings.transport_pro_max_search_pages,
                )
                for record in records:
                    # The search asked for posted loads, so that is the provenance
                    # when a summary record carries no flag of its own.
                    load = self._map(record, posted=True)
                    if load is None or load.load_id in seen:
                        skipped += 1
                        continue
                    # Re-checked per record: a filter the endpoint ignored would
                    # otherwise put another office's freight in the agent's mouth.
                    if not self._in_scope(load, scope):
                        logger.warning(
                            "Load %s came back from a terminalId=%s search but "
                            "belongs to terminal %s — the filter was not applied. "
                            "Dropping it.", load.load_id, terminal, load.terminal_id)
                        skipped += 1
                        continue
                    if load.is_bookable and load.is_quotable:
                        seen.add(load.load_id)
                        loads.append(load)
                        if len(loads) >= cap:
                            break
                    else:
                        skipped += 1
        except TransportProError as exc:
            raise SourceUnavailable(f"open load search failed: {exc}") from exc

        if skipped:
            logger.info(
                "Skipped %d load(s) across %d search(es): not %s, posting off, no "
                "published rate, or out of office scope.", skipped, searched,
                " / ".join(sorted(self._settings.open_load_statuses)) or "(none)")
        if not loads:
            logger.error(
                "No sellable loads found across %d search(es)%s. If the board looks "
                "fine in Transport Pro, the status vocabulary probably differs from "
                "TRANSPORT_PRO_OPEN_LOAD_STATUSES (%r) — run `lanevoice-tpcheck "
                "--load <id> --raw` to see the real value.",
                searched,
                f" of {scope.title(scope.root_id)}'s terminals" if scope.configured
                else "",
                self._settings.transport_pro_open_load_statuses)
        self._loads.put("__open__", loads)
        return loads

    # -- carriers ----------------------------------------------------------- #
    def get_carrier(self, mc_or_dot: str) -> Carrier | None:
        """Vet a number the caller gave us against Transport Pro.

        The API takes MC and DOT as separate parameters and the caller rarely
        labels which they said, so both are tried. MC goes first: it is what a
        carrier volunteers on a sales call.
        """
        number = _digits(mc_or_dot)
        if len(number) < 4:
            return None
        hit, cached = self._carriers.get(number)
        if hit:
            return cached

        # Start the enrichment lookups NOW, keyed on the number as heard, while
        # Transport Pro answers. A carrier volunteers their MC, so when the MC
        # probe hits these are the very lookups `_enrich_carrier` would make next
        # — and they have been running the whole time. When the caller gave a DOT
        # the record's MC will not match what was heard, the results are
        # discarded and the lookups run again on the right identifiers: two
        # wasted reads on the rare path against a whole round trip saved on the
        # common one. Before this, Transport Pro, then Highway, then HappyRobot
        # ran one after another, on the one turn where a caller has just read out
        # their number and is waiting to hear whether it worked.
        speculative = _Speculative(
            keyed_on=number,
            highway=(self._lookups.submit(self._highway_record, mc=number, dot=None)
                     if self._highway is not None else None),
            happyrobot=(self._lookups.submit(self._happyrobot_record, mc=number, dot=None)
                        if self._happyrobot is not None else None),
        )

        try:
            record = self._client.carrier_status(mc_number=number)
            if record is None:
                record = self._client.carrier_status(dot_number=number)
        except TransportProError as exc:
            raise SourceUnavailable(
                f"carrier lookup for {mc_or_dot} failed: {exc}") from exc

        looked_up = None
        if record is None:
            # `/voiceai/carrier_status` does not have every carrier the desk knows
            # about — MC 1798414 is absent from it and present, as an explicit
            # FAIL, on the HappyRobot endpoint. Without this fallback that carrier
            # read as "we could not capture your number", got asked twice more and
            # was handed to a rep, when the honest answer was a decline.
            looked_up = self._carrier_lookup_record(
                number, mc_probe=speculative.happyrobot)
            record = looked_up

        carrier = None
        if record is not None:
            carrier = map_carrier(
                record,
                insurance_reported_only=(
                    self._settings.transport_pro_require_insurance_field),
            )
            # Enrich BEFORE caching, so the extra lookups are paid once per
            # carrier per TTL rather than once per call. `looked_up` is handed
            # through so the fallback's response is reused rather than fetched a
            # second time — the same call, on the critical path, twice.
            carrier = self._enrich_carrier(carrier, number, looked_up=looked_up,
                                           speculative=speculative)
        if carrier is not None:
            if not carrier.authority_reported:
                logger.error(
                    "Transport Pro carrier_status for %s carried no status field "
                    "this code recognises (fields present: %s) — routing to a "
                    "human rather than declining the carrier. Add the real field "
                    "name to _STATUS_KEYS in mappers.py.",
                    number, sorted(record)[:15])
            if carrier.carrier_id:
                self._carrier_ids[carrier.usdot_number] = carrier.carrier_id
            # Cache under every number the caller might repeat it as.
            for alias in {number, _digits(carrier.mc_number or ""),
                          _digits(carrier.usdot_number)}:
                if len(alias) >= 4:
                    self._carriers.put(alias, carrier)
        else:
            self._carriers.put(number, None)
        return carrier

    def _carrier_lookup_record(self, number: str, *,
                               mc_probe: Future | None = None) -> dict | None:
        """The HappyRobot record for a carrier `/voiceai/carrier_status` lacks.

        Its rows carry the same fields `map_carrier` already reads — `status`,
        `carrier_name`, `mc_number`, `us_dot_number`, `id` — so nothing special is
        needed to map one. Crucially it carries a STATUS, including an explicit
        `FAIL`, which is what turns an unknown caller into a definite answer.

        `mc_probe` is the lookup by MC that `get_carrier` started while Transport
        Pro was still answering; it is the first half of this fallback, already in
        flight, so it is collected rather than repeated.

        Never raises: this is a fallback, and a carrier we cannot look up at all
        must stay "not found" (a re-ask, then a rep) rather than becoming an error
        mid-call.
        """
        if self._happyrobot is None:
            return None
        record = (mc_probe.result() if mc_probe is not None
                  else self._happyrobot_record(mc=number, dot=None))
        if record is None:
            record = self._happyrobot_record(mc=None, dot=number)
        if record is _LOOKUP_FAILED:
            return None
        if record is not None:
            logger.info(
                "Carrier %s is not on /voiceai/carrier_status but IS on the "
                "HappyRobot endpoint as %r — using that.",
                number, record.get("status"))
        return record

    def _highway_record(self, *, mc: str | None, dot: str | None) -> Any:
        """Highway's record; None when Highway has no such carrier; `_LOOKUP_FAILED`
        when the call itself failed. Never raises — this is enrichment, not the
        gate — which is also what makes it safe to run on a worker thread."""
        try:
            return self._highway.carrier(mc=mc or None, dot=None if mc else dot)
        except (HighwayError, ValueError) as exc:
            logger.warning(
                "Highway lookup failed for MC %s / DOT %s (%s) — vetting falls "
                "back to Transport Pro's list.", mc or "-", dot or "-", exc)
            return _LOOKUP_FAILED

    def _happyrobot_record(self, *, mc: str | None, dot: str | None) -> Any:
        """HappyRobot's `carrier_lookup` row, None, or `_LOOKUP_FAILED` — same
        contract as `_highway_record`."""
        try:
            return self._happyrobot.carrier_lookup(mc=mc or None, dot=None if mc else dot)
        except (HappyRobotError, ValueError) as exc:
            logger.warning(
                "HappyRobot carrier_lookup failed for MC %s / DOT %s (%s) — "
                "Highway's verdicts stand alone.", mc or "-", dot or "-", exc)
            return _LOOKUP_FAILED

    def _enrich_carrier(self, carrier: Carrier | None, heard: str, *,
                        looked_up: dict | None = None,
                        speculative: _Speculative | None = None) -> Carrier | None:
        """Add what `/voiceai/carrier_status` doesn't carry: qualifications.

        That endpoint returns only {carrier_name, city, state, dot_number,
        mc_number, id, status} — verified against the live tenant — so on its own
        the agent cannot tell whether a carrier is allowed to haul a load that
        demands Critical Cargo. Two sources fill the gap:

          Highway `rules_assessment`   the AUTHORITY, pass/fail per classification
          HappyRobot `carrier_lookup`  the list Transport Pro holds, used only
                                       where Highway has no verdict

        Two extra round trips, which is why they sit inside the cached
        `get_carrier` (once per carrier per `TRANSPORT_PRO_CARRIER_CACHE_SECONDS`)
        and behind shorter timeouts than the Transport Pro calls themselves — and
        why they run in PARALLEL: with each other, and, via `speculative`, with
        the Transport Pro probe that preceded this call. `speculative` holds the
        lookups `get_carrier` started on the number as heard; they are used only
        when the record confirms that number is the MC both APIs are keyed on.

        Never raises, and never turns a found carrier into None. Both sources are
        enrichment: unreachable, misconfigured, no record, or an unrecognised
        shape all leave the carrier as Transport Pro described them. Letting an
        outage in either decline live carriers would be worse than the
        stale-classification problem they are here to fix.
        """
        if carrier is None:
            return carrier

        # MC first: it is what a carrier volunteers, and both APIs key on it. Fall
        # back to the digits the caller actually read out when the record carries
        # no MC of its own.
        mc = _digits(carrier.mc_number or "")
        dot = _digits(carrier.usdot_number or "")
        updates: dict[str, object] = {}

        # The lookups started on the heard number are the right ones exactly when
        # the record says that number IS the MC. Otherwise start the correct pair
        # now — still alongside each other rather than one after the other.
        reuse = (speculative is not None and bool(mc)
                 and mc.lstrip("0") == speculative.keyed_on.lstrip("0"))
        by = {"mc": mc or None, "dot": None if mc else (dot or heard)}
        highway_f: Future | None = None
        if self._highway is not None:
            highway_f = (speculative.highway
                         if reuse and speculative.highway is not None
                         else self._lookups.submit(self._highway_record, **by))
        hr_f: Future | None = None
        if looked_up is None and self._happyrobot is not None:
            hr_f = (speculative.happyrobot
                    if reuse and speculative.happyrobot is not None
                    else self._lookups.submit(self._happyrobot_record, **by))

        if highway_f is not None:
            record = highway_f.result()
            if record is _LOOKUP_FAILED:
                record = None
            if record is not None:
                if assessment := highway_mappers.classifications(record):
                    updates["highway_assessment"] = assessment
                if overall := highway_mappers.overall_result(record):
                    updates["highway_overall_result"] = overall
                    if overall == "fail":
                        logger.info(
                            "Highway's overall verdict on MC %s is FAIL — the "
                            "carrier does not clear its rules.", mc or dot)
                if (limit := highway_mappers.cargo_insurance_limit(record)) is not None:
                    updates["cargo_insurance_limit"] = limit
                # The trading name, for the read-back. Transport Pro frequently
                # returns a PERSON here (an owner-operator's own name) where
                # Highway has the company, and the agent is told to confirm
                # carriers by COMPANY name.
                if self._settings.highway_prefer_company_name:
                    name = highway_mappers.company_name(record)
                    if name and name != carrier.legal_name:
                        logger.info(
                            "Using Highway's company name %r for MC %s (Transport "
                            "Pro said %r) — the agent confirms by company name.",
                            name, mc or dot, carrier.legal_name)
                        updates["legal_name"] = name

        # The classification list Transport Pro's `carrier_status` doesn't carry.
        # Reuse the fallback's response when there is one: without this it would be
        # the same request twice, on the critical path of a live call.
        hr_record = looked_up
        if hr_record is None and hr_f is not None:
            hr_record = hr_f.result()
            if hr_record is _LOOKUP_FAILED:
                hr_record = None
        if isinstance(hr_record, dict):
            listed = hr_record.get("classifications")
            if isinstance(listed, list):
                held = tuple(dict.fromkeys(
                    c.strip() for c in listed
                    if isinstance(c, str) and c.strip()))
                if held:
                    updates["qualifications"] = held

        if not updates:
            return carrier
        logger.debug("Enriched carrier MC %s: %s", mc or dot, sorted(updates))
        return dataclasses.replace(carrier, **updates)

    def carriers_matching_digits(self, digits: str, limit: int = 5) -> list[Carrier]:
        """Not supported by this API — see the module docstring."""
        return []

    # -- the carrier's address file ----------------------------------------- #
    def carrier_emails(self, usdot_number: str) -> tuple[str, ...]:
        """Addresses on the carrier's Transport Pro record, oldest first.

        This is the set the booking gate checks against, so it is deliberately
        ONLY what Transport Pro holds. Merging in addresses captured on earlier
        calls would let an address the desk never accepted pass the gate the
        second time somebody read it out.
        """
        hit, cached = self._emails.get(usdot_number)
        if hit:
            return cached

        carrier_id = self._carrier_ids.get(usdot_number)
        if not carrier_id:
            # We only reach here for a carrier we already looked up, so a missing
            # id means the status payload had no id field to key contacts on.
            logger.warning(
                "No Transport Pro carrier_id for USDOT %s, so /contact/search "
                "cannot be called and no address can be verified.", usdot_number)
            self._emails.put(usdot_number, ())
            return ()
        try:
            emails = contact_emails(self._client.carrier_contacts(carrier_id))
        except TransportProError as exc:
            raise SourceUnavailable(
                f"contact lookup for carrier {carrier_id} failed: {exc}") from exc
        self._emails.put(usdot_number, emails)
        return emails

    def email_on_file(self, usdot_number: str, email: str) -> bool:
        return email.strip().lower() in self.carrier_emails(usdot_number)

    def add_carrier_email(self, usdot_number: str, email: str) -> bool:
        """Not written back. The Public API has no create-contact endpoint.

        Returning False keeps the caller honest: nothing was added, so nothing
        should be said about having saved it. The address is still captured in
        the call note and in the offer we post, which is where a rep will look.
        """
        logger.info("Not adding %s to Transport Pro for USDOT %s: the Public API "
                    "has no contact-create endpoint. Captured in the call note.",
                    email, usdot_number)
        return False

    # -- writes Transport Pro owns ------------------------------------------ #
    def record_booking(
        self,
        load: Load,
        carrier: Carrier,
        rate: float,
        *,
        email: str | None = None,
        phone: str | None = None,
        contact_name: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Put the agreed rate on the load as an offer. False if it didn't land.

        This LOGS a rate for a rep to action. It is not, by itself, a booking —
        see `booking_link`, which is what actually produces something the carrier
        can act on. Kept as the fallback path for a deployment with no HappyRobot
        credentials, where a logged offer is the most the agent can achieve.
        """
        if not (email or phone):
            logger.error("Not posting the offer on load %s: Transport Pro requires "
                         "an email or a phone number and we have neither.",
                         load.load_id)
            return False
        try:
            self._client.make_offer(
                load.load_id,
                carrier_name=carrier.legal_name,
                contact_name=contact_name or carrier.legal_name,
                offer_amount=rate,
                email=email,
                phone_number=phone,
                mc_number=carrier.mc_number,
                dot_number=carrier.usdot_number,
                carrier_id=carrier.carrier_id,
                notes=notes,
            )
            logger.info("Transport Pro: offer of $%s posted on load %s for %s",
                        int(rate), load.load_id, carrier.legal_name)
            self._loads.clear()   # its status just changed underneath us
            return True
        except (TransportProError, ValueError) as exc:
            logger.error("Transport Pro: could not post the $%s offer on load %s "
                         "for %s: %s", int(rate), load.load_id,
                         carrier.legal_name, exc)
            return False

    def booking_link(
        self,
        load: Load,
        carrier: Carrier,
        rate: float,
        *,
        email: str | None = None,
        phone: str | None = None,
        contact_name: str | None = None,
        notes: str | None = None,
    ) -> BookingAttempt:
        """The URL the carrier opens to actually take the load.

        Two writes, in this order, and the order matters:

            POST /offer   -> offer_id      (the rate is now on the record)
            accept_offer  -> book_now_url  (the carrier can now sign)

        The load is NOT the carrier's until they open that link and sign. Nothing
        here books anything — which is precisely why the link exists, and why the
        agent must say "open it and sign to lock it in" rather than "you're
        booked". A carrier told they are booked, who then loses the load because
        they took an hour over the link, was told something false by us.

        A `BookingAttempt` with no url means no link, for one of three reasons, and
        `offer_recorded` is what tells them apart:

          * HappyRobot isn't configured -> nothing recorded; `record_booking` is
                                           the fallback path
          * `POST /offer` failed        -> nothing recorded; the lane is untouched
          * accept_offer gave no URL    -> the offer IS on the record, so a rep can
                                           finish it — and must not duplicate it

        Never raises: by the time this runs the carrier has already agreed a rate,
        and the call must end in something coherent whatever the TMS does.
        """
        if self._happyrobot is None:
            logger.info(
                "No HappyRobot credentials, so no booking link can be produced for "
                "load %s. Logging the offer instead — a rep finishes it.",
                load.load_id)
            return BookingAttempt()
        if not (email or phone):
            logger.error("Not creating an offer on load %s: an email or phone is "
                         "required and we have neither.", load.load_id)
            return BookingAttempt()

        try:
            offer_id = self._client.create_offer(
                load.load_id,
                carrier_name=carrier.legal_name,
                contact_name=contact_name or carrier.legal_name,
                offer_amount=rate,
                mc_number=carrier.mc_number,
                carrier_id=carrier.carrier_id,
                email=email,
                phone=phone,
                comments=notes or "",
                record_as_user_id=self._settings.transport_pro_booking_user_id,
            )
        except (TransportProError, ValueError) as exc:
            logger.error("Could not create the $%s offer on load %s for %s: %s",
                         int(rate), load.load_id, carrier.legal_name, exc)
            return BookingAttempt()
        if not offer_id:
            logger.error("POST /offer returned no offer id for load %s — no link.",
                         load.load_id)
            return BookingAttempt()

        try:
            url = self._happyrobot.accept_offer(offer_id)
        except (HappyRobotError, ValueError) as exc:
            # The offer EXISTS at this point. Say so in the log, or whoever reads
            # it will assume nothing was recorded and double-book the lane.
            logger.error(
                "Offer %s of $%s is recorded on load %s, but accept_offer failed "
                "(%s) so no booking link went out. A rep can finish it from the "
                "offer.", offer_id, int(rate), load.load_id, exc)
            return BookingAttempt(offer_id=offer_id)

        self._loads.clear()   # the load's status just moved underneath us
        if not url:
            return BookingAttempt(offer_id=offer_id)
        logger.info("Booking link issued for load %s at $%s for %s (offer %s)",
                    load.load_id, int(rate), carrier.legal_name, offer_id)
        return BookingAttempt(url=url, offer_id=offer_id)

    def invite_to_onboard(self, carrier: Carrier) -> bool:
        """Send the Highway connect invite to a carrier who passed vetting.

        Only for `AuthorityStatus.NOT_CONNECTED`. The address is taken from the
        carrier's FILE, never from what a caller said on the phone: an unverified
        address plus a broker-branded onboarding link is a phishing message aimed
        at a real carrier, and refusing to trust a spoken address is the entire
        point of the email gate.
        """
        if self._happyrobot is None:
            logger.info("No HappyRobot credentials, so no Highway invite can be "
                        "sent to %s.", carrier.legal_name)
            return False
        if not carrier.mc_number:
            logger.warning("Cannot invite %s to onboard: no MC number on the "
                           "record, and invite_carrier is keyed on MC.",
                           carrier.legal_name)
            return False

        on_file = self.carrier_emails(carrier.usdot_number)
        if not on_file:
            logger.warning(
                "Not sending a Highway invite to %s: no address on their Transport "
                "Pro file, and an address heard on the call is exactly what must "
                "not be trusted here.", carrier.legal_name)
            return False
        try:
            return self._happyrobot.invite_carrier(
                mc_number=carrier.mc_number, email=on_file[0])
        except (HappyRobotError, ValueError, SourceUnavailable) as exc:
            logger.error("Could not send the Highway invite to %s: %s",
                         carrier.legal_name, exc)
            return False

    def record_capacity(
        self,
        carrier: Carrier,
        *,
        equipment_type: str,
        origin_city: str,
        origin_state: str,
        date_available: str,
        email: str | None = None,
        phone: str | None = None,
        contact_name: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """The empty call, as a capacity row on the carrier.

        Every argument above the optionals is required by the API. A failure here
        must never affect the call: the carrier told us where their truck is, and
        whether our TMS filed it is our problem, not theirs.
        """
        if not (email or phone):
            logger.info("Skipping carrier capacity for %s: no email or phone yet.",
                        carrier.legal_name)
            return False
        try:
            self._client.add_carrier_capacity(
                carrier_name=carrier.legal_name,
                contact_name=contact_name or carrier.legal_name,
                equipment_type=equipment_type,
                origin_city=origin_city,
                origin_state=origin_state,
                date_available=date_available,
                email=email,
                phone_number=phone,
                mc_number=carrier.mc_number,
                dot_number=carrier.usdot_number,
                carrier_id=carrier.carrier_id,
                notes=notes,
            )
            return True
        except (TransportProError, ValueError) as exc:
            logger.warning("Transport Pro: could not record capacity for %s: %s",
                           carrier.legal_name, exc)
            return False

    def post_load_note(self, load_id: str, content: str) -> bool:
        """Write a note onto the load so a rep sees what happened on the call."""
        try:
            self._client.add_load_note(load_id, content)
            return True
        except TransportProError as exc:
            logger.warning("Transport Pro: could not add a note to load %s: %s",
                           load_id, exc)
            return False

    # -- local audit trail (delegated verbatim) ----------------------------- #
    def get_rep(self, rep_id: str) -> Rep | None:
        """The rep behind an id a load carries.

        The desk's own directory (`reps.toml`) is consulted first: it is where a
        direct number or an availability flag lives when Transport Pro's record
        has only the office main line. Then Transport Pro's user record, so that
        the carrier sales rep on a load resolves to a real name and number with
        no directory entry at all. Never raises — an unreadable user costs the
        named handoff, and the call falls back to any available rep.
        """
        local = self._audit.get_rep(rep_id)
        if local is not None:
            return local
        if not str(rep_id).strip().isdigit():
            return None                       # a directory-only id, not a Transport Pro one
        hit, cached = self._reps.get(rep_id)
        if hit:
            return cached
        try:
            record = self._client.user(rep_id)
        except TransportProError as exc:
            logger.warning("Transport Pro user %s could not be read (%s) — the call "
                           "falls back to any available rep.", rep_id, exc)
            return None
        rep = map_rep(record)
        self._reps.put(rep_id, rep)
        return rep

    def available_rep(self, exclude_rep_id: str | None = None) -> Rep | None:
        return self._audit.available_rep(exclude_rep_id)

    def start_call(self, call_id: str) -> None:
        self._audit.start_call(call_id)

    def log_offer(self, call_id: str, round_number: int, party: OfferParty,
                  amount: float) -> None:
        self._audit.log_offer(call_id, round_number, party, amount)

    def log_note(self, call_id: str, note: str) -> None:
        self._audit.log_note(call_id, note)

    def set_caller_number(self, call_id: str, number: str) -> None:
        self._audit.set_caller_number(call_id, number)

    def offers_for_call(self, call_id: str) -> list[tuple[int, str, float]]:
        return self._audit.offers_for_call(call_id)

    def log_transfer(self, call_id: str, rep_id: str, result: str) -> None:
        self._audit.log_transfer(call_id, rep_id, result)

    def book_load(self, load_id: str) -> None:
        """Local bookkeeping only — `record_booking` is what tells Transport Pro."""
        self._audit.book_load(load_id)
        self._loads.clear()

    def end_call(self, call_id: str, load_id: str | None, carrier_dot: str | None,
                 outcome: str, transcript: list | str, carrier_name: str | None = None,
                 carrier_mc: str | None = None, end_label: str | None = None,
                 end_reason: str | None = None) -> None:
        self._audit.end_call(call_id, load_id, carrier_dot, outcome, transcript,
                             carrier_name, carrier_mc, end_label, end_reason)

    def update_transcript(self, call_id: str, transcript: list | str) -> None:
        self._audit.update_transcript(call_id, transcript)


def _match(records: list[dict], load_id: str) -> dict | None:
    """The record for exactly this load.

    `search_available` takes `load_id` as a filter, but it is a search endpoint:
    it may answer with several rows, and a filter it doesn't recognise is liable
    to be ignored rather than rejected. Confirming the id ourselves is what stops
    the agent pitching load 1303370 to somebody who asked about 1303369.

    Both spellings are checked: the Voice AI feed calls it `load_id`, the load
    endpoints call it `id`. Matching only the first quietly turns every real load
    payload into "no such load on the board".
    """
    wanted = _digits(load_id)
    for record in records:
        for key in ("load_id", "id"):
            if (value := record.get(key)) is not None and _digits(str(value)) == wanted:
                return record
    return None
