"""Typed domain models and value objects (PRD §10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LoadStatus(str, Enum):
    OPEN = "open"
    COVERED = "covered"
    CANCELLED = "cancelled"
    # On the board but not in a state the desk sells: awaiting an appointment, on
    # hold, still being quoted, planned but not released. Kept apart from COVERED
    # because the agent says something different for each, and telling a caller a
    # load is "already covered" when it simply isn't ready yet is a false
    # statement they may well repeat to the shipper.
    NOT_READY = "not_ready"


class AuthorityStatus(str, Enum):
    """Carrier vetting status as the source system reports it.

    Only ACTIVE clears the desk — that is the company requirement, so every other
    value is a hard stop. Anything unrecognised is coerced to SUSPENDED rather
    than raising: a lookup that blows up mid-call is bad, but a typo or a new feed
    value silently reading as good authority is far worse. Fail closed, never
    guess ACTIVE.

    Transport Pro's `/voiceai/carrier_status` reports three values in practice —
    `ACTIVE`, `FAIL` and `REVIEW`. The last one is why PENDING exists: a carrier
    part-way through onboarding has not failed anything, and telling them they
    don't meet our requirements is both untrue and the kind of thing they repeat
    to other brokers. They go to a human instead (see `CarrierVerificationService`).
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"   # suspended, revoked, out-of-service, failed vetting
    PENDING = "pending"       # onboarding not finished — a person decides

    @classmethod
    def _missing_(cls, value: object) -> AuthorityStatus:
        raw = str(value).strip().lower().replace("-", " ").replace("_", " ")
        raw = " ".join(raw.split())
        if raw in {"active", "authorized", "authorized for property", "a", "pass",
                   "passed", "approved", "ok"}:
            return cls.ACTIVE
        if raw in {"inactive", "not in operation", "dormant", "none", "i"}:
            return cls.INACTIVE
        if raw in {"review", "in review", "pending", "pending review", "onboarding",
                   "incomplete", "new"}:
            return cls.PENDING
        # fail, failed, suspended, revoked, out of service, do not use, anything
        # we have never seen.
        return cls.SUSPENDED

    @property
    def can_haul(self) -> bool:
        """The single gate: ACTIVE authority, nothing else."""
        return self is AuthorityStatus.ACTIVE

    @property
    def is_definite(self) -> bool:
        """True when the source gave a settled answer, good or bad.

        PENDING is the one that isn't: the carrier is mid-onboarding, so the
        honest response is a handoff rather than a refusal.
        """
        return self is not AuthorityStatus.PENDING


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
    # Reefer setpoint, e.g. "-20 F". A condition of taking the load rather than a
    # detail — a driver who agrees to haul ice cream without hearing "minus
    # twenty" has agreed to something else.
    temperature: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == LoadStatus.OPEN

    @property
    def is_bookable(self) -> bool:
        return self.is_open and self.is_posted

    @property
    def is_quotable(self) -> bool:
        """True if there is a real rate range to negotiate inside.

        A load can be posted and open and still arrive with no published Load
        Board Rate. There is no honest anchor to open at in that case, so the
        agent hands it to a rep instead of inventing one — a made-up opening
        number is a number the desk may have to honour.
        """
        return self.open_rate > 0 and self.ceiling_rate >= self.open_rate

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
            ("Temperature", self.temperature),
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

    # Transport Pro's own id for the carrier record, when we came from there.
    # `/contact/search` is keyed on it, so without it we cannot read their
    # address file.
    carrier_id: str | None = None

    # The status string the source system actually sent, before it was folded
    # into `authority_status`. `None` means the record carried no status field we
    # could find — which is a very different thing from a record that says
    # "inactive", and is routed to a human instead of declined. Kept verbatim so
    # the reason in the call log is the source's word, not our interpretation.
    raw_authority_status: str | None = None

    @property
    def latest_email(self) -> str | None:
        return self.contact_emails[-1] if self.contact_emails else None

    @property
    def authority_reported(self) -> bool:
        """False when we could not find a vetting status on the record at all."""
        return self.raw_authority_status is not None


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
