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

Two further gates apply once authority is settled, and only when a load is given:
whether the carrier holds the classifications THIS load demands, and whether the
freight is worth more than their cargo cover. Both route to a human rather than
declining — a load requirement is a fact about the freight, not a judgement on the
carrier — and both fail SAFE in the other direction too: a qualification we cannot
establish is not treated as one the carrier lacks unless the load actually asks
for it, and an unreadable insurance limit skips the value check rather than
blocking it.

Where carriers come from (SQLite seed or the Transport Pro API) makes no
difference here — that is the repository's job, including which of Highway and
Transport Pro supplied a given qualification. This logic is the same either way,
which is what lets it be tested offline against hand-built `Carrier` objects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lanevoice.domain.models import (
    AuthorityStatus,
    Load,
    VerificationAction,
    VerificationResult,
)
from lanevoice.logging_config import get_logger

if TYPE_CHECKING:
    from lanevoice.db.repository import Repository

logger = get_logger(__name__)

# A carrier whose authority was reactivated within this window is a fraud signal.
_REACTIVATION_WINDOW_DAYS = 90


class CarrierVerificationService:
    def __init__(self, repo: Repository):
        self._repo = repo

    def verify(self, mc_or_dot: str, load: Load | None = None) -> VerificationResult:
        """Vet the carrier, and — when a load is given — vet them FOR THAT LOAD.

        `load` is optional so the desk gate still works on its own, but the call
        flow always has one by this point: the load number comes before the MC.
        Without it the two load-specific gates below cannot run, and a carrier can
        be cleared for a load they are not qualified to haul.
        """
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

        # Highway's verdict on the carrier AS A WHOLE. Checked before the source
        # system's own status because it is the stricter and better-informed
        # answer: Highway sees authority, insurance, safety rating and ELD, and a
        # `fail` from it means the carrier does not clear the rules at all.
        #
        # This is the override direction that matters commercially. MC 1798414 is
        # absent from `/voiceai/carrier_status`, reads FAIL on the HappyRobot
        # endpoint, and fails every Highway classification with
        # `needs_to_connect_eld` — and before this gate existed the call ended in
        # "let me get you to a rep" rather than a decline.
        #
        # Only an explicit "fail" decides anything here. "review", "pass" and a
        # missing verdict all fall through to the source system, so an unreachable
        # Highway changes nothing.
        if carrier.highway_overall_result == "fail":
            logger.info(
                "Highway's overall verdict on %s (%s) is FAIL — declining. "
                "classifications=%s",
                carrier.legal_name, carrier.mc_number or carrier.usdot_number,
                dict(carrier.highway_assessment))
            return VerificationResult(
                verified=False,
                action=VerificationAction.DECLINE,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=(*risk_flags, "highway_overall_fail"),
                reason="authority_not_active",
            )

        # Passed the vetting rules, hasn't connected with us (Transport Pro's
        # `PASS`). The one non-active status with a specific remedy rather than a
        # judgement: there is nothing to decide and nothing wrong with them, they
        # simply have no agreement with us to haul under. So the agent sends the
        # Highway invite and hands over — a rep can often walk them through it
        # while they are still on the line.
        #
        # Checked BEFORE `is_definite` because both land there and they are not
        # the same situation: REVIEW needs a decision, this needs a link.
        if carrier.authority_status is AuthorityStatus.NOT_CONNECTED:
            return VerificationResult(
                verified=False,
                action=VerificationAction.HUMAN_REVIEW,
                carrier=carrier,
                high_risk=True,
                approved=carrier.approved,
                risk_flags=tuple(risk_flags),
                reason="onboarding_not_connected",
                invite_to_onboard=True,
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

        # --- authority is ACTIVE from here. Now: are they right for THIS load? -- #
        #
        # These two run after the authority gates so a suspended carrier is
        # labelled by the suspension rather than by a qualification they were never
        # going to be asked about. Both route to a human rather than declining:
        # a load requirement is a fact about the freight, not a judgement on the
        # carrier, and a rep can sometimes get an override where the agent can't.
        if load is not None:
            missing = tuple(
                required for required in load.required_classifications
                if not carrier.qualifies_for(required)
            )
            if missing:
                logger.info(
                    "Load %s requires %s; %s does not qualify for %s "
                    "(holds=%s, highway=%s). Routing to a rep.",
                    load.load_id, list(load.required_classifications),
                    carrier.legal_name, list(missing),
                    list(carrier.qualifications), dict(carrier.highway_assessment))
                return VerificationResult(
                    verified=True,   # they ARE who they say; just not for this load
                    action=VerificationAction.HUMAN_REVIEW,
                    carrier=carrier,
                    high_risk=True,
                    approved=carrier.approved,
                    risk_flags=(*risk_flags,
                                f"unqualified[{','.join(missing)}]"),
                    reason="qualification_not_met",
                )

            # The freight is worth more than the carrier's cargo cover. Only
            # enforced when BOTH numbers are known — a load that declares no value
            # or a carrier whose policy we couldn't read skips the check rather
            # than being blocked by it. Roughly 2% of the live board declares a
            # value, so this fires rarely and must not misfire.
            if load.commodity_value and carrier.cargo_insurance_limit is not None:
                if load.commodity_value > carrier.cargo_insurance_limit:
                    logger.info(
                        "Load %s declares $%s of freight; %s carries $%s of cargo "
                        "cover. Routing to a rep.", load.load_id,
                        int(load.commodity_value), carrier.legal_name,
                        int(carrier.cargo_insurance_limit))
                    return VerificationResult(
                        verified=True,
                        action=VerificationAction.HUMAN_REVIEW,
                        carrier=carrier,
                        high_risk=True,
                        approved=carrier.approved,
                        risk_flags=(*risk_flags, "commodity_value_over_cover"),
                        reason="commodity_value_over_cover",
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
