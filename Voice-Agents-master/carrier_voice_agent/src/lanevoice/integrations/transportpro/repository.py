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

* The FALLBACK rep list is local. The rep a load is assigned to comes from
  Transport Pro — `internalContacts` names them, `GET /user/{id}` gives the name
  and phone (`get_rep`) — but "whoever is free" cannot: the API has no presence
  or on-call concept, so `available_rep` still reads the same seeded table it
  always did. That table is the §9.5 fallback, not the primary answer.
"""

from __future__ import annotations

import datetime
import threading
import time
from collections.abc import Iterable
from typing import Any

from lanevoice.db.repository import Repository
from lanevoice.domain.errors import SourceUnavailable
from lanevoice.domain.models import Carrier, Load, OfferParty, Rep
from lanevoice.integrations.transportpro.client import (
    TransportProClient,
    TransportProError,
)
from lanevoice.integrations.transportpro.mappers import (
    contact_emails,
    map_carrier,
    map_load,
    map_rep,
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
    ):
        self._client = client
        self._audit = audit
        self._settings = settings
        self._loads = _TTLCache(settings.transport_pro_load_cache_seconds)
        self._carriers = _TTLCache(settings.transport_pro_carrier_cache_seconds)
        self._emails = _TTLCache(settings.transport_pro_carrier_cache_seconds)
        # A rep's name and desk phone move about as often as a carrier's vetting
        # status, so they share its expiry rather than getting a knob of their own.
        self._reps = _TTLCache(settings.transport_pro_carrier_cache_seconds)
        # usdot (as the rest of the system keys carriers) -> Transport Pro id,
        # so `carrier_emails` can reach `/contact/search` from a USDOT alone.
        self._carrier_ids: dict[str, str] = {}

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
        if load is not None and load.assigned_rep_id is None:
            logger.warning(
                "Transport Pro load %s names no carrier sales rep in its "
                "internalContacts, so a caller who asks for the rep on this load "
                "gets whoever is free instead of its owner. If the load DOES show a "
                "carrier rep in Transport Pro, the contact type is spelled something "
                "this build does not recognise — the mapper logged the types the "
                "load carried; put the right one in "
                "TRANSPORT_PRO_CARRIER_REP_CONTACT_TYPES.", load.load_id)
        self._loads.put(key, load)
        return load

    def _map(self, record: dict, *, posted: bool) -> Load | None:
        """Map a load record under this deployment's sellability rules."""
        return map_load(
            record,
            posted=posted,
            fraud_low_ratio=self._settings.transport_pro_fraud_low_ratio,
            open_statuses=self._settings.open_load_statuses,
            rep_types=self._settings.carrier_rep_contact_types,
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
        try:
            records = self._client.search_loads(
                pickup_date_start=today.isoformat(),
                pickup_date_end=horizon.isoformat(),
                load_status=wanted[0] if len(wanted) == 1 else None,
                is_posted=True,
            )
        except TransportProError as exc:
            raise SourceUnavailable(f"open load search failed: {exc}") from exc

        loads, skipped = [], 0
        for record in records:
            # The search asked for posted loads, so that is the provenance when a
            # summary record carries no flag of its own.
            load = self._map(record, posted=True)
            if load is not None and load.is_bookable and load.is_quotable:
                loads.append(load)
                if len(loads) >= self._settings.transport_pro_max_offered_loads:
                    break
            else:
                skipped += 1
        if skipped:
            logger.info(
                "Skipped %d of %d loads on the board: not %s, posting off, or no "
                "published rate.", skipped, len(records),
                " / ".join(sorted(self._settings.open_load_statuses)) or "(none)")
        if not loads and records:
            logger.error(
                "None of the %d loads on the board are sellable. If they look fine "
                "in Transport Pro, the status vocabulary probably differs from "
                "TRANSPORT_PRO_OPEN_LOAD_STATUSES (%r) — run `lanevoice-tpcheck "
                "--load <id> --raw` to see the real value.",
                len(records), self._settings.transport_pro_open_load_statuses)
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

        try:
            record = self._client.carrier_status(mc_number=number)
            if record is None:
                record = self._client.carrier_status(dot_number=number)
        except TransportProError as exc:
            raise SourceUnavailable(
                f"carrier lookup for {mc_or_dot} failed: {exc}") from exc

        carrier = None
        if record is not None:
            carrier = map_carrier(
                record,
                insurance_reported_only=(
                    self._settings.transport_pro_require_insurance_field),
            )
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

        `make_offer` is what "booked" means through this API — there is no
        separate book endpoint — and it is also what triggers the confirmation
        Transport Pro sends to the carrier's address.
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

    # -- reps: the person a load belongs to --------------------------------- #
    def get_rep(self, rep_id: str) -> Rep | None:
        """The rep behind a load's `assigned_rep_id`.

        On a Transport Pro load that id is a Transport Pro USER id, lifted from
        the load's `internalContacts`, so it is resolved with `GET /user/{id}`.
        A non-numeric id is a local rep id from the seeded fallback list and goes
        to the audit repository, which is what keeps the offline reps reachable in
        a deployment that has both.

        **This never raises.** It is called from `TransferService`, which the agent
        reaches from its failure paths — including the one that already handles
        `SourceUnavailable` — so an exception here would turn a handoff into a
        dropped call. A lookup that fails returns None, and the transfer falls back
        to whoever is free (§9.5).
        """
        key = str(rep_id or "").strip()
        if not key:
            return None
        if not key.isdigit():
            return self._audit.get_rep(key)

        hit, cached = self._reps.get(key)
        if hit:
            return cached

        try:
            record = self._client.user(key)
        except TransportProError as exc:
            # Deliberately not cached: the next call on this load should try again
            # rather than inherit one bad minute for the rest of the TTL.
            logger.warning(
                "Could not look up Transport Pro user %s to transfer a call to: "
                "%s. Falling back to an available rep.", key, exc)
            return None

        rep = map_rep(record) if record is not None else None
        if rep is None:
            logger.warning(
                "Transport Pro has no user %s, but a load is assigned to them. "
                "Falling back to an available rep.", key)
        self._reps.put(key, rep)
        return rep

    # -- local audit trail (delegated verbatim) ----------------------------- #
    def available_rep(self, exclude_rep_id: str | None = None) -> Rep | None:
        """The §9.5 fallback: whoever is free. Local — the API has no presence."""
        return self._audit.available_rep(exclude_rep_id)

    def available_reps(self, exclude: Iterable[str] = ()) -> list[Rep]:
        """The fallback queue, for a rep who doesn't take the whisper."""
        return self._audit.available_reps(exclude)

    def start_call(self, call_id: str) -> None:
        self._audit.start_call(call_id)

    def log_offer(self, call_id: str, round_number: int, party: OfferParty,
                  amount: float) -> None:
        self._audit.log_offer(call_id, round_number, party, amount)

    def log_note(self, call_id: str, note: str) -> None:
        self._audit.log_note(call_id, note)

    def log_transfer(self, call_id: str, rep_id: str, result: str) -> None:
        self._audit.log_transfer(call_id, rep_id, result)

    def book_load(self, load_id: str) -> None:
        """Local bookkeeping only — `record_booking` is what tells Transport Pro."""
        self._audit.book_load(load_id)
        self._loads.clear()

    def end_call(self, call_id: str, load_id: str | None, carrier_dot: str | None,
                 outcome: str, transcript: list | str) -> None:
        self._audit.end_call(call_id, load_id, carrier_dot, outcome, transcript)


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
