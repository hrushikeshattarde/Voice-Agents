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
    # Transport Pro's `PASS`: the carrier cleared the vetting RULES but has not
    # connected with us on Highway yet, so there is no agreement to haul under.
    # Deliberately NOT folded into ACTIVE — "passed vetting" and "cleared to
    # book" are different claims, and reading PASS as ACTIVE means booking a
    # carrier we have no signed relationship with. It is also the one non-active
    # status with a specific remedy: send the Highway invite (see
    # `CarrierVerificationService` and `VerificationResult.invite_to_onboard`).
    NOT_CONNECTED = "not_connected"

    @classmethod
    def _missing_(cls, value: object) -> AuthorityStatus:
        raw = str(value).strip().lower().replace("-", " ").replace("_", " ")
        raw = " ".join(raw.split())
        if raw in {"active", "authorized", "authorized for property", "a",
                   "approved", "ok"}:
            return cls.ACTIVE
        # `pass` lands here, NOT on ACTIVE — see NOT_CONNECTED above.
        if raw in {"pass", "passed", "not connected", "not onboarded",
                   "invited", "connect pending"}:
            return cls.NOT_CONNECTED
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

        Two values aren't: PENDING (mid-review) and NOT_CONNECTED (passed the
        rules, hasn't connected). Neither has failed anything, so the honest
        response to both is a handoff rather than a refusal.
        """
        return self not in (AuthorityStatus.PENDING, AuthorityStatus.NOT_CONNECTED)


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

    # -- qualification gates: what the CARRIER must hold to haul this ------- #
    # These two are inputs to vetting, NOT things a caller is ever told, so they
    # are deliberately absent from `facts()`. Reading a load's declared value out
    # loud invites a different conversation entirely, and reciting the
    # classifications a carrier has to hold tells them which answer to give.
    #
    # `reference.requiredClassifications`, e.g. ("Critical Cargo",). Populated on
    # roughly one posted load in ten on the live tenant, so an empty tuple means
    # "nothing extra required", never "we couldn't tell".
    required_classifications: tuple[str, ...] = ()
    # Which office/POD owns this load (`assignedTerminal`), as a string because
    # the two Transport Pro endpoints type it differently. None when the payload
    # didn't say. A deployment scoped to one office refuses to sell anything
    # outside it — see `TerminalScope`. Never spoken: the carrier does not care
    # which POD it is, and naming internal structure on a sales call is noise.
    terminal_id: str | None = None

    # Where the pickup physically is, from the waypoint's own `latitude` /
    # `longitude`. Used to estimate how far a caller's empty truck is from it.
    # None on plenty of real records — a stop can carry a city with null
    # coordinates — and the deadhead estimate is simply skipped for those rather
    # than guessed from the city name.
    origin_lat: float | None = None
    origin_lon: float | None = None
    # `reference.commodityValue` — the declared value of the freight, checked
    # against the carrier's cargo insurance limit. None when the load doesn't
    # declare one, which is the common case.
    commodity_value: float | None = None

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

    # -- what the carrier is qualified to haul ------------------------------- #
    # Classifications the SOURCE SYSTEM lists them as holding, e.g.
    # ("Interstate", "Critical Cargo"). Compared against a load's
    # `required_classifications`.
    qualifications: tuple[str, ...] = field(default_factory=tuple)
    # Highway's own `rules_assessment`, as (classification, result) pairs where
    # result is "pass" / "fail" / "review". A tuple rather than a dict so the
    # dataclass stays hashable and genuinely frozen.
    #
    # Empty means Highway was not consulted or could not be reached, which
    # degrades to the source system's list — never to a refusal.
    highway_assessment: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Highway's `rules_assessment.overall_result` — "pass" / "fail" / "review", its
    # verdict on the carrier as a whole rather than per classification. A "fail"
    # here is a definite no and is treated as one: MC 1798414 came back with every
    # classification failing and `needs_to_connect_eld`, which is exactly the
    # "doesn't meet our requirements" case. None when Highway wasn't consulted or
    # had no record, which must NOT read as a failure.
    highway_overall_result: str | None = None
    # Highest ACTIVE motor_truck_cargo policy limit, from Highway. None when we
    # could not read one, in which case the commodity-value gate SKIPS rather
    # than blocks: a lookup failure must not decline a legitimate carrier.
    cargo_insurance_limit: float | None = None

    @property
    def latest_email(self) -> str | None:
        return self.contact_emails[-1] if self.contact_emails else None

    @property
    def authority_reported(self) -> bool:
        """False when we could not find a vetting status on the record at all."""
        return self.raw_authority_status is not None

    def qualifies_for(self, classification: str) -> bool:
        """Does the carrier hold `classification`? Highway wins where it speaks.

        Highway is authoritative in BOTH directions, which is the whole point of
        consulting it — the source system's list has been observed wrong each way:

          Highway "pass" -> qualified, even if the source's list omits it
          Highway "fail" -> NOT qualified, even if the source's list claims it
          "review" / absent -> no opinion, fall back to the source's list

        Matched case-insensitively: the two systems disagree on capitalisation of
        the same classification often enough that an exact compare silently drops
        qualifications the carrier really holds.
        """
        wanted = classification.strip().lower()
        for name, result in self.highway_assessment:
            if name.strip().lower() != wanted:
                continue
            verdict = (result or "").strip().lower()
            if verdict == "pass":
                return True
            if verdict == "fail":
                return False
            break   # "review" — Highway has no opinion; fall through
        return any(q.strip().lower() == wanted for q in self.qualifications)


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
    # True only for a NOT_CONNECTED carrier: they passed vetting and the one thing
    # standing between them and this load is a Highway connection, so the agent
    # sends the invite before handing over. Never set for a carrier who FAILED
    # something — inviting them to onboard would be the wrong message entirely.
    invite_to_onboard: bool = False


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
