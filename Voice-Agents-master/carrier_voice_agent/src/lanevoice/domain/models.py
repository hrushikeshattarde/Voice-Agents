"""Typed domain models and value objects (PRD §10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LoadStatus(str, Enum):
    OPEN = "open"
    COVERED = "covered"
    CANCELLED = "cancelled"


class AuthorityStatus(str, Enum):
    """Carrier operating authority as the vetting feed reports it.

    Only ACTIVE clears the desk — that is the company requirement, so INACTIVE
    and SUSPENDED are both hard stops. Anything the feed sends that we don't
    recognise is coerced to SUSPENDED rather than raising: a lookup that blows up
    mid-call is bad, but a typo or a new feed value silently reading as good
    authority is far worse. Fail closed, never guess ACTIVE.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"   # suspended, revoked, out-of-service, failed vetting

    @classmethod
    def _missing_(cls, value: object) -> AuthorityStatus:
        raw = str(value).strip().lower().replace("-", " ").replace("_", " ")
        raw = " ".join(raw.split())
        if raw in {"active", "authorized", "authorized for property", "a", "pass"}:
            return cls.ACTIVE
        if raw in {"inactive", "not in operation", "dormant", "none", "i"}:
            return cls.INACTIVE
        return cls.SUSPENDED

    @property
    def can_haul(self) -> bool:
        """The single gate: ACTIVE authority, nothing else."""
        return self is AuthorityStatus.ACTIVE


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

    # The things a carrier actually asks about once they hear the lane: how far,
    # what's on it, when does it deliver, what are the appointment windows. A rep
    # volunteers all of it in one breath; without it the agent gets interrogated.
    miles: int | None = None
    commodity: str | None = None
    pieces: int | None = None
    dimensions: str | None = None        # "3.5 ft long x 50 in wide x 97 in high"
    pickup_window: str | None = None     # "6 AM to 3 PM"
    delivery_date: str | None = None
    delivery_window: str | None = None
    load_type: str = "full truckload"

    @property
    def is_open(self) -> bool:
        return self.status == LoadStatus.OPEN

    @property
    def is_bookable(self) -> bool:
        return self.is_open and self.is_posted

    def facts(self, today: object | None = None) -> str:
        """Everything speakable about this load, as labelled facts.

        This is DATA, not a script — it goes to the LLM, which decides the wording
        and the order. Anything we don't know is left out entirely rather than
        sent as "unknown", so the model can't read a gap as something to mention.
        Rates are deliberately absent: those come from the negotiation engine.
        """
        from lanevoice.formatting import spoken_date

        rows = [
            ("Load number", self.load_id),
            ("Type", self.load_type),
            ("Origin", self.origin),
            ("Destination", self.destination),
            ("Picks up", spoken_date(self.pickup_date, today)),
            ("Pickup window", self.pickup_window),
            ("Delivers", spoken_date(self.delivery_date, today)
                if self.delivery_date else None),
            ("Delivery window", self.delivery_window),
            ("Commodity", self.commodity),
            ("Pieces", self.pieces),
            ("Dimensions", self.dimensions),
            ("Weight", f"{self.weight_lbs:,} lbs" if self.weight_lbs else None),
            ("Equipment needed", self.equipment),
            ("Miles", f"{self.miles:,}" if self.miles else None),
            ("Special requirements", self.notes),
        ]
        return "\n".join(f"{k}: {v}" for k, v in rows if v not in (None, ""))


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
