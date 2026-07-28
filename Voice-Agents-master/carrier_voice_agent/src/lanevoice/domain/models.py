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
    HOLD = "hold"           # carrier hasn't moved — restate our number
    PULL = "pull"           # carrier moved — credit it, ask them to come closer
    COUNTER = "counter"
    REVIEW = "review"
    ESCALATE = "escalate"    # within Max Buy but above the agent's own authority
    NO_DEAL = "no_deal"


class VerificationAction(str, Enum):
    PROCEED = "proceed"
    HUMAN_REVIEW = "human_review"
    DECLINE = "decline"          # carrier not approved to work with us


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
    open_rate: float        # Load Board Rate — the floor the agent opens/anchors at
    ceiling_rate: float     # Max Buy — the hard cap; the agent never goes above it
    fraud_low_rate: float   # suspiciously cheap -> fraud review
    assigned_rep_id: str | None
    status: LoadStatus
    is_posted: bool = True   # only proceed with posted loads
    notes: str | None = None  # special requirements to read to the carrier

    @property
    def is_open(self) -> bool:
        return self.status == LoadStatus.OPEN

    @property
    def is_bookable(self) -> bool:
        return self.is_open and self.is_posted


@dataclass(frozen=True)
class Carrier:
    usdot_number: str
    mc_number: str | None
    legal_name: str
    authority_status: AuthorityStatus
    insurance_on_file: bool
    authority_reactivated_days: int | None = None
    last_verified_at: str | None = None
    approved: bool = True   # approved to work with Circle Logistics
    # Every address we know for them, newest last. A caller's address is checked
    # against these and appended if new — carriers legitimately have several.
    contact_emails: tuple[str, ...] = field(default_factory=tuple)

    @property
    def latest_email(self) -> str | None:
        return self.contact_emails[-1] if self.contact_emails else None


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
    approved: bool = True
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None


@dataclass(frozen=True)
class NegotiationResult:
    decision: Decision
    rate: float | None = None
    reason: str | None = None
    final_offer: float | None = None
    within_ceiling: bool | None = None
    is_final: bool = False   # this counter is the agent's best/last offer
    is_split: bool = False   # this counter is a meet-in-the-middle close
    hold_number: int = 0     # 1 = first push-back; 2+ = carrier still hasn't moved
    pull_number: int = 0     # 1 = first "how close can you get?"; 2+ = pressing again


@dataclass(frozen=True)
class TransferResolution:
    rep: Rep | None
    is_fallback: bool
    note: str | None = None
