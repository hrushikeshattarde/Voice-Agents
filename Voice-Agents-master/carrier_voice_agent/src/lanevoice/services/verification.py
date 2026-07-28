"""
Carrier verification (PRD §8) — the gate every call passes before a rate exists.

The desk requirement is a single sentence: the carrier's MC/USDOT has to be in
the system as ACTIVE. INACTIVE and SUSPENDED are both hard stops, and so is any
status the source sent that we couldn't read as one of the three
(`AuthorityStatus` fails closed to SUSPENDED — it never guesses ACTIVE).

The outcomes are deliberately three, not two, because they are three different
things and a carrier hears something different for each:

    PROCEED       active, insured, on file       -> the load comes out
    DECLINE       the source says not active     -> they don't meet the
                                                   requirements to work with us
    HUMAN_REVIEW  we don't KNOW that they're     -> a person looks at it
                  not active

That last row is the one worth guarding. "Their authority is inactive" and "we
could not find a status field on their record" are the same absence of a yes, but
telling a legitimate carrier they fail our requirements because our own mapping
missed a field is a false accusation, and it is the kind of thing a carrier
repeats to other brokers. `Carrier.authority_reported` separates them: a record
whose status we could not read at all goes to a human.

Where carriers come from (SQLite seed or the Transport Pro API) makes no
difference here — that is the repository's job. This logic is the same either way.
"""

from __future__ import annotations

from lanevoice.db.repository import Repository
from lanevoice.domain.models import VerificationAction, VerificationResult

# A carrier whose authority was reactivated within this window is a fraud signal.
_REACTIVATION_WINDOW_DAYS = 90


class CarrierVerificationService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def verify(self, mc_or_dot: str) -> VerificationResult:
        carrier = self._repo.get_carrier(mc_or_dot)
        if carrier is None:
            # Could equally be a misheard digit as a carrier we don't have, so
            # this is not a decline — the agent asks again, then hands it over.
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                reason="not_found",
            )

        risk_flags: list[str] = []
        if not carrier.authority_reported:
            risk_flags.append("authority_not_reported")
        elif not carrier.authority_status.can_haul:
            risk_flags.append(
                f"authority_{carrier.authority_status.value}"
                f"[{carrier.raw_authority_status}]")
        if not carrier.insurance_on_file:
            risk_flags.append("insurance_lapse")
        if (
            carrier.authority_reactivated_days is not None
            and carrier.authority_reactivated_days <= _REACTIVATION_WINDOW_DAYS
        ):
            risk_flags.append("recently_reactivated")

        # We couldn't read a status at all — that's our problem, not theirs.
        if not carrier.authority_reported:
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=tuple(risk_flags),
                reason="authority_not_reported",
            )

        # Mid-onboarding (Transport Pro's `REVIEW`). Not a failure — nothing has
        # been decided yet — so it is not a decline. Onboarding can often finish
        # this on the call, which is exactly what a rep is for.
        if not carrier.authority_status.is_definite:
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=tuple(risk_flags),
                reason="authority_pending_review",
            )

        # The source system says they are not active. This is the one the caller
        # is told about, in the vague terms the desk uses: they don't currently
        # meet the requirements to work with us.
        if not carrier.authority_status.can_haul:
            return VerificationResult(
                verified=False,
                action=VerificationAction.DECLINE,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=tuple(risk_flags),
                reason="authority_not_active",
            )

        # Active authority but no insurance on file. Also a hard stop, but it is
        # routinely a paperwork lag rather than a dead carrier, and a rep can
        # often fix it on the call — so it goes to a person, not to a decline.
        if not carrier.insurance_on_file:
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=tuple(risk_flags),
                reason="insurance_lapse",
            )

        # Authority and insurance are fine. Are they approved to work with us?
        if not carrier.approved:
            return VerificationResult(
                verified=True,
                action=VerificationAction.DECLINE,
                carrier=carrier,
                approved=False,
                risk_flags=tuple(risk_flags),
                reason="not_approved",
            )

        # Verified, but a soft risk flag routes to a human (logged, never dropped).
        return VerificationResult(
            verified=True,
            action=(
                VerificationAction.HUMAN_REVIEW if risk_flags
                else VerificationAction.PROCEED
            ),
            carrier=carrier,
            high_risk=bool(risk_flags),
            approved=True,
            risk_flags=tuple(risk_flags),
        )
