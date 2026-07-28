"""
Carrier verification service (PRD §8).

This is a MOCK backed by the seed DB. In production, replace the lookup with a
call to FMCSA QCMobile + a commercial fallback (Highway / RMIS / DAT), but keep
this exact decision + fraud-flag logic. USDOT is treated as primary (§8.3).
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
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                reason="not_found",
            )

        risk_flags: list[str] = []
        # ACTIVE authority is the company requirement. INACTIVE and SUSPENDED both
        # stop here, and so does any status the feed sent that we couldn't read
        # (AuthorityStatus fails closed to SUSPENDED).
        if not carrier.authority_status.can_haul:
            risk_flags.append(f"authority_{carrier.authority_status.value}")
        if not carrier.insurance_on_file:
            risk_flags.append("insurance_lapse")
        if (
            carrier.authority_reactivated_days is not None
            and carrier.authority_reactivated_days <= _REACTIVATION_WINDOW_DAYS
        ):
            risk_flags.append("recently_reactivated")

        hard_fail = (
            not carrier.authority_status.can_haul
            or not carrier.insurance_on_file
        )

        if hard_fail:
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=tuple(risk_flags),
                reason="authority_or_insurance",
            )

        # Authority/insurance are fine, but is the carrier approved to work with us?
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
