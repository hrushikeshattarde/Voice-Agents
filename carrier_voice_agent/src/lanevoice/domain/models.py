"""Typed domain models and value objects (PRD §10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LoadStatus(str, Enum):
    OPEN = "open"
    COVERED = "covered"
    CANCELLED = "cancelled"


class AuthorityStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    INACTIVE = "inactive"


class CallOutcome(str, Enum):
    BOOKED = "booked"
    TRANSFERRED = "transferred"
    REJECTED = "rejected"
    NO_DEAL = "no_deal"
    ABANDONED = "abandoned"


class OfferParty(str, Enum):
    CARRIER = "carrier"
    AGENT = "agent"


class Decision(str, Enum):
    ACCEPT = "accept"
    HOLD = "hold"
    COUNTER = "counter"
    REVIEW = "review"
    NO_DEAL = "no_deal"


class VerificationAction(str, Enum):
    PROCEED = "proceed"
    HUMAN_REVIEW = "human_review"


# --------------------------------------------------------------------------- #
# Persistent entities
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Load:
    load_id: str
    origin: str
    destination: str
    pickup_date: str
    equipment: str
    weight_lbs: int
    open_rate: float        # advertised/opening (low) offer the agent states
    ceiling_rate: float     # absolute max we will ever pay
    fraud_low_rate: float   # suspiciously cheap -> fraud review
    assigned_rep_id: str | None
    status: LoadStatus

    @property
    def is_open(self) -> bool:
        return self.status == LoadStatus.OPEN


@dataclass(frozen=True)
class Carrier:
    usdot_number: str
    mc_number: str | None
    legal_name: str
    authority_status: AuthorityStatus
    insurance_on_file: bool
    authority_reactivated_days: int | None = None
    last_verified_at: str | None = None


@dataclass(frozen=True)
class Rep:
    rep_id: str
    name: str
    phone: str
    available: bool


# --------------------------------------------------------------------------- #
# Service result value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    action: VerificationAction
    carrier: Carrier | None = None
    high_risk: bool = False
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None


@dataclass(frozen=True)
class NegotiationResult:
    decision: Decision
    rate: float | None = None
    reason: str | None = None
    final_offer: float | None = None
    within_ceiling: bool | None = None


@dataclass(frozen=True)
class TransferResolution:
    rep: Rep | None
    is_fallback: bool
    note: str | None = None
