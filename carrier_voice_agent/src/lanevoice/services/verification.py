"""
Carrier verification service (PRD §8).

This is a MOCK backed by the seed DB. In production, replace the lookup with a
call to FMCSA QCMobile + a commercial fallback (Highway / RMIS / DAT), but keep
this exact decision + fraud-flag logic. USDOT is treated as primary (§8.3).
"""

from __future__ import annotations

from lanevoice.db.repository import Repository
from lanevoice.domain.models import (
    AuthorityStatus,
    VerificationAction,
    VerificationResult,
)

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
        if carrier.authority_status != AuthorityStatus.ACTIVE:
            risk_flags.append(f"authority_{carrier.authority_status.value}")
        if not carrier.insurance_on_file:
            risk_flags.append("insurance_lapse")
        if (
            carrier.authority_reactivated_days is not None
            and carrier.authority_reactivated_days <= _REACTIVATION_WINDOW_DAYS
        ):
            risk_flags.append("recently_reactivated")

        hard_fail = (
            carrier.authority_status != AuthorityStatus.ACTIVE
            or not carrier.insurance_on_file
        )

        if hard_fail:
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                carrier=carrier,
                high_risk=True,
                risk_flags=tuple(risk_flags),
                reason="authority_or_insurance",
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
            risk_flags=tuple(risk_flags),
        )
