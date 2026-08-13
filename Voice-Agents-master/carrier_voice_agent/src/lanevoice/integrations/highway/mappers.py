"""
Highway JSON -> the three things the vetting gate needs.

Kept separate from the client so the translation is testable against a real
payload without HTTP, same as the Transport Pro mappers next door.

Everything here is deliberately forgiving. Highway is an ENRICHMENT source: a
field that moved, a shape that changed, or a policy record we can't parse must
degrade to "no opinion" so the call falls back to Transport Pro's answer. The one
thing that would be unforgivable is reading a missing field as a `fail` and
declining a legitimate carrier on our own parsing bug.
"""

from __future__ import annotations

from typing import Any

from lanevoice.logging_config import get_logger

logger = get_logger(__name__)

# The results Highway reports per classification. Anything else is treated as
# "no opinion" rather than guessed at.
_KNOWN_RESULTS = ("pass", "fail", "review")


def classifications(record: Any) -> tuple[tuple[str, str], ...]:
    """`rules_assessment.classifications` -> (("Critical Cargo", "pass"), ...).

    Order is preserved and duplicates are dropped on first-seen, so a feed that
    lists a classification twice can't produce two conflicting verdicts for it.
    Unrecognised result strings are dropped entirely: `Carrier.qualifies_for`
    treats an absent classification as "fall back to Transport Pro", which is the
    safe reading of a value we don't understand.
    """
    if not isinstance(record, dict):
        return ()
    assessment = record.get("rules_assessment")
    if not isinstance(assessment, dict):
        return ()
    entries = assessment.get("classifications")
    if not isinstance(entries, list):
        return ()

    found: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        result = entry.get("result")
        if not isinstance(name, str) or not name.strip():
            continue
        verdict = str(result or "").strip().lower()
        if verdict not in _KNOWN_RESULTS:
            logger.debug("Highway classification %r has result %r, which this code "
                         "does not recognise — ignoring it.", name, result)
            continue
        found.setdefault(name.strip(), verdict)
    return tuple(found.items())


def overall_result(record: Any) -> str | None:
    """`rules_assessment.overall_result` — Highway's verdict on the whole carrier.

    "pass" / "fail" / "review", or None when Highway said nothing we recognise.
    None is important: it must read as "no opinion", never as a failure, or an
    unreachable Highway would start declining live carriers.

    This is coarser than `classifications` and answers a different question. A
    carrier can fail every classification and still be someone the desk works
    with; `overall_result: "fail"` is Highway saying they do not clear its rules at
    all. MC 1798414 is the worked example — every classification failing, and
    `summary.carrier_actions_to_improve_rules_result: "needs_to_connect_eld"`.
    """
    if not isinstance(record, dict):
        return None
    assessment = record.get("rules_assessment")
    if not isinstance(assessment, dict):
        return None
    verdict = str(assessment.get("overall_result") or "").strip().lower()
    return verdict if verdict in _KNOWN_RESULTS else None


def cargo_insurance_limit(record: Any) -> float | None:
    """Highest ACTIVE `motor_truck_cargo` policy limit, or None.

    None is a real and common answer, and the caller must treat it as "skip the
    commodity-value check" rather than "no coverage" — a carrier with a policy we
    failed to parse is not a carrier without insurance.

    Active policies are preferred, but when a carrier has cargo policies and none
    is marked active we fall back to the whole set rather than to None: the status
    vocabulary here is not documented, and "we saw a $100k cargo policy" beats
    "we saw nothing" for a check whose only job is catching a load worth more
    than the coverage.
    """
    if not isinstance(record, dict):
        return None
    insurance = record.get("insurance")
    if not isinstance(insurance, dict):
        return None
    policies = insurance.get("insurance_policies")
    if not isinstance(policies, list):
        return None

    cargo = [p for p in policies
             if isinstance(p, dict) and p.get("is_type") == "motor_truck_cargo"]
    if not cargo:
        return None
    active = [p for p in cargo
              if str(p.get("status") or "").strip().lower() == "active"] or cargo

    limits: list[float] = []
    for policy in active:
        # Observed as the STRING "100000.0" on the live tenant, not a number.
        try:
            limit = float(str(policy.get("limit")).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            continue
        if limit > 0:
            limits.append(limit)
    return max(limits) if limits else None


def company_name(record: Any) -> str | None:
    """The carrier's trading name, for the read-back on the phone.

    Transport Pro's `carrier_name` is often the owner's personal name — MC 1594669
    comes back as "Victor Hugo Vargas Aguilar" there and
    "LOS AGUILARES TRANSPORTATION" here. The agent is told to confirm carriers by
    COMPANY name, so a person's name read back as "is this "Victor Hugo Vargas
    Aguilar"?" is both wrong and slightly unsettling to hear.

    Title-cased when the feed SHOUTS, because this string goes to a TTS voice and
    an all-caps name is a coin toss whether it gets spelled out letter by letter.
    """
    if not isinstance(record, dict):
        return None
    for key in ("dba_name", "legal_name", "name", "carrier_name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            name = " ".join(value.split())
            return name.title() if name.isupper() else name
    return None
