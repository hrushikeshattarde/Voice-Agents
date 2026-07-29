"""
`lanevoice-tpcheck` — prove the Transport Pro wiring before pointing a phone at it.

    lanevoice-tpcheck                          auth only
    lanevoice-tpcheck --mc 343195              auth + vet a carrier
    lanevoice-tpcheck --load 1303369 --mc 343195 --raw

Read-only: it authenticates, looks things up, and prints what came back. It never
posts an offer, a note or a capacity row.

It exists for three failure modes that are invisible until a live call, and silent
when they happen:

* **A load status vocabulary mismatch.** The agent sells only `Ready To Dispatch`,
  and the API's own endpoints disagree about the wording — the collection's
  `/voiceai/load/search_available` example answers `AVAILABLE`. Get this wrong and
  a full board reads as empty: the agent offers nothing and never says why. The
  tool prints the status it actually saw next to the accepted set.
* **An unreadable `carrier_status` payload.** That endpoint has no saved example
  response in the collection, so `mappers.py` finds its fields by name across
  whatever shape arrives. If a status can't be found the carrier is sent to a
  human, which looks like a policy decision rather than a bug.
* **A carrier-rep contact type we don't recognise.** A load names its people as
  `internalContacts` types, and no saved payload in the collection shows the
  carrier-rep one. Get it wrong and a caller asking for "the rep on this load" is
  quietly transferred to whoever is free instead of to the load's owner — a
  handoff that works, to the wrong desk. The tool prints the types the load
  actually carries next to the rep it resolved.

`--raw` prints the real payloads next to what the mappers made of them, which is
how you confirm both in about ten seconds. When something is wrong the output
names the setting or the constant to change.
"""

from __future__ import annotations

import argparse
import json
import sys

from lanevoice.domain.models import LoadStatus
from lanevoice.env import load_env
from lanevoice.integrations.transportpro.client import (
    TransportProClient,
    TransportProError,
)
from lanevoice.integrations.transportpro.mappers import (
    _load_status_text,
    _posting_flag,
    contact_emails,
    map_carrier,
    map_load,
    map_rep,
)
from lanevoice.logging_config import setup_logging
from lanevoice.settings import get_settings

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def _show(status: str, message: str) -> None:
    print(f"[{status}] {message}")


def _dump(label: str, payload: object) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(payload, indent=2, default=str)[:4000])
    print("---\n")


def _check_carrier(client, number: str, raw: bool) -> None:
    """Vet one MC/USDOT exactly the way a live call does."""
    record = client.carrier_status(mc_number=number)
    probed = "mc_number"
    if record is None:
        record = client.carrier_status(dot_number=number)
        probed = "dot_number"
    if record is None:
        _show(WARN, f"carrier {number}: not found as an MC or a USDOT number")
        return

    _show(OK, f"carrier {number}: found (matched on {probed})")
    if raw:
        _dump(f"raw carrier_status for {number}", record)

    carrier = map_carrier(record)
    if carrier is None:
        _show(BAD, "  the record carries no MC or DOT number this code can find. "
                   "Re-run with --raw and check _MC_KEYS / _DOT_KEYS in mappers.py")
        return

    print(f"       name              : {carrier.legal_name}")
    print(f"       MC / USDOT        : {carrier.mc_number} / {carrier.usdot_number}")
    print(f"       transport pro id  : {carrier.carrier_id or '(none)'}")
    print(f"       raw status        : {carrier.raw_authority_status!r}")
    print(f"       read as           : {carrier.authority_status.value}")
    print(f"       can haul for us   : {'YES' if carrier.authority_status.can_haul else 'NO'}")

    if not carrier.authority_reported:
        _show(BAD, "  NO STATUS FIELD FOUND on this record. The agent will send "
                   "these callers to a human rather than vet them. Re-run with "
                   "--raw, find the real field name, and add it to _STATUS_KEYS "
                   "in mappers.py.")
    if not carrier.carrier_id:
        _show(WARN, "  no carrier id on the record, so /contact/search cannot be "
                    "called for this carrier and NO booking can be confirmed for "
                    "them. Check _ID_KEYS in mappers.py.")
        return

    contacts = client.carrier_contacts(carrier.carrier_id)
    emails = contact_emails(contacts)
    if emails:
        _show(OK, f"  {len(emails)} address(es) on the account: {', '.join(emails)}")
        print("       a booking is only ever confirmed to one of these.")
    else:
        _show(WARN, "  no email addresses on this carrier's account — the booking "
                    "gate will refuse every address they give and hand the call to "
                    "a rep.")
    if raw and contacts:
        _dump(f"raw contact/search for carrier {carrier.carrier_id}", contacts[:3])


def _check_rep(client, record: dict, load, settings, raw: bool) -> None:
    """Who a caller asking for "the rep on this load" would actually reach.

    Two hops, and either can be the reason a handoff lands on the wrong desk: the
    load has to name a carrier rep in a type this build recognises, and that user
    has to have a number we can dial. Both are printed with the values seen, so a
    vocabulary mismatch is a one-line env change rather than a mystery.
    """
    contacts = record.get("internalContacts")
    kinds = [str(c.get("type")) for c in contacts or [] if isinstance(c, dict)]
    print(f"       internal contacts : {', '.join(kinds) or '(none)'}")

    if load.assigned_rep_id is None:
        _show(BAD, "  no carrier sales rep on this load, so a caller who asks for "
                   "the rep on it is handed to whoever is free instead. If "
                   "Transport Pro DOES show a carrier rep, add its type from the "
                   "line above to TRANSPORT_PRO_CARRIER_REP_CONTACT_TYPES "
                   f"(currently: {settings.transport_pro_carrier_rep_contact_types}).")
        return

    user = client.user(load.assigned_rep_id)
    if raw and user:
        _dump(f"raw GET /user/{load.assigned_rep_id}", user)
    rep = map_rep(user) if user else None
    if rep is None:
        _show(BAD, f"  the load is assigned to user {load.assigned_rep_id}, but "
                   f"GET /user/{load.assigned_rep_id} returned nothing usable. The "
                   "transfer will fall back to whoever is free.")
        return

    print(f"       assigned rep      : {rep.name or '(no name)'} "
          f"({rep.title or 'no title'})")
    print(f"       transfer would go : {rep.spoken_phone or '(nowhere)'}")
    if rep.available:
        _show(OK, "  a caller asking for the rep on this load reaches them")
    else:
        _show(BAD, "  no dialable number on their user record, so the transfer "
                   "falls back to whoever is free. Check phoneNumbers in --raw.")
    if rep.extension:
        _show(WARN, f"  their number carries extension {rep.extension}, which "
                    "cannot travel in a SIP transfer. The call reaches the "
                    "switchboard and the extension goes in the call note.")


def _check_load(client, load_id: str, raw: bool) -> None:
    """Look a load number up the way `_identify_load` does — `GET /load/{id}`."""
    match = client.load_detail(load_id)
    if match is None:
        _show(WARN, f"load {load_id}: no such load — the agent would offer the "
                    "caller the open ones instead")
        return

    _show(OK, f"load {load_id}: found")
    if raw:
        _dump(f"raw GET /load/{load_id}", match)

    settings = get_settings()
    # `posted=False` mirrors the repository: this endpoint serves any load, so a
    # payload with no posting flag has to read as "not on the board".
    load = map_load(match, posted=False,
                    fraud_low_ratio=settings.transport_pro_fraud_low_ratio,
                    open_statuses=settings.open_load_statuses,
                    rep_types=settings.carrier_rep_contact_types)
    if load is None:
        _show(BAD, "  the record has no load_id this code can find")
        return

    # The two sellability conditions, with the values actually seen. A status
    # vocabulary mismatch is the likeliest reason a healthy board reads as empty,
    # so it is reported before anything else.
    raw_status = _load_status_text(match) or "(none)"
    accepted = ", ".join(sorted(settings.open_load_statuses)) or "(none)"
    print(f"       status on record  : {raw_status!r}")
    print(f"       statuses we sell  : {accepted}")
    if load.status is LoadStatus.OPEN:
        _show(OK, "  status is one the agent sells")
    else:
        _show(BAD, f"  status {raw_status!r} is NOT sellable (read as "
                   f"{load.status.value}). The agent will refuse this load. If "
                   f"this status should be sellable, set "
                   f"TRANSPORT_PRO_OPEN_LOAD_STATUSES to include it.")

    flag = _posting_flag(match)
    if flag is True:
        _show(OK, "  isPosted = true on the record")
    elif flag is False:
        _show(BAD, "  isPosted = false — the agent will not offer this load")
    else:
        _show(WARN, "  the record carries no isPosted field. GET /load/{id} serves "
                    "any load regardless of posting, so this reads as NOT posted "
                    "and the agent will not offer it. Check postingInfo in --raw.")

    print(f"       lane              : {load.origin} -> {load.destination}")
    print(f"       picks up          : {load.pickup_date} {load.pickup_window or ''}")
    print(f"       delivers          : {load.delivery_date or '?'} "
          f"{load.delivery_window or ''}")
    print(f"       equipment / miles : {load.equipment or '?'} / {load.miles or '?'}")
    print(f"       commodity         : {load.commodity or '(none)'}")
    print(f"       floor (opens at)  : ${int(load.open_rate)}")
    print(f"       max buy (cap)     : ${int(load.ceiling_rate)}")
    print(f"       fraud tripwire    : ${int(load.fraud_low_rate)}")
    print(f"       board notes       : {load.notes or '(none)'}")

    if not load.is_quotable:
        _show(BAD, "  no usable Load Board Rate, so there is no honest number to "
                   "open at. The agent will hand this load to a rep.")
    if not load.origin or not load.destination:
        _show(WARN, "  the lane is incomplete — check the waypoints in --raw")

    # Who a caller asking for a person on this load gets put through to.
    _check_rep(client, match, load, settings, raw)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the Transport Pro connection and how its payloads map.")
    parser.add_argument("--mc", help="an MC or USDOT number to vet")
    parser.add_argument("--load", help="a load number to look up")
    parser.add_argument("--raw", action="store_true",
                        help="print the raw payloads alongside the mapping")
    args = parser.parse_args()

    load_env()
    settings = get_settings()
    setup_logging(settings.log_level)

    missing = [name for name in ("transport_pro_url", "transport_pro_username",
                                 "transport_pro_password")
               if not getattr(settings, name)]
    if missing:
        _show(BAD, "not configured: " + ", ".join(n.upper() for n in missing))
        print("\nAdd them to .env — see .env.example.")
        return 2

    print(f"Transport Pro : {settings.transport_pro_url}")
    print(f"User          : {settings.transport_pro_username}\n")

    try:
        with TransportProClient(settings) as client:
            client._authorize()
            _show(OK, "authenticated (POST /auth)")
            if args.load:
                _check_load(client, args.load, args.raw)
            if args.mc:
                _check_carrier(client, args.mc, args.raw)
            if not (args.load or args.mc):
                print("\nPass --load and/or --mc to check a real record.")
    except TransportProError as exc:
        _show(BAD, str(exc))
        return 1

    print("\nDone. Nothing was written to Transport Pro.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
