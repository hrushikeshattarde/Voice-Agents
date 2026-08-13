"""
Vetting the carrier FOR THIS LOAD, not just in general.

Transport Pro's `/voiceai/carrier_status` carries no classification list at all —
verified against the live tenant, it returns only {carrier_name, city, state,
dot_number, mc_number, id, status}. So before these gates existed the agent could
agree a rate with a carrier holding no qualification to haul the freight. Roughly
one posted load in ten on the live board demands a classification, so the hole was
not theoretical.

Two principles run through everything here:

  * A load requirement is a fact about the FREIGHT, not a judgement on the
    carrier. So an unmet requirement routes to a human, never to a decline.
  * Every "we could not establish this" degrades to the safe side. An unreadable
    insurance limit skips the value check; an unreachable Highway falls back to
    Transport Pro's list. Neither ever declines a carrier on our own bug.

All offline: hand-built `Carrier` objects over the seeded fixture, which is
exactly what `verification.py` staying source-agnostic buys us.
"""

import dataclasses

from lanevoice.domain.models import AuthorityStatus, VerificationAction
from lanevoice.services import CarrierVerificationService


def _service(carrier):
    """A verification service over one hand-built carrier, no repository."""
    service = CarrierVerificationService(None)
    service._repo = type("_R", (), {"get_carrier": lambda _s, _n: carrier})()
    return service


def _load(repo, **overrides):
    return dataclasses.replace(repo.get_load("L1001"), **overrides)


def _carrier(repo, **overrides):
    """The seeded active/insured/approved carrier, adjusted."""
    return dataclasses.replace(repo.get_carrier("MC123456"), **overrides)


# --------------------------------------------------------------------------- #
# Required classifications
# --------------------------------------------------------------------------- #
def test_a_load_with_no_requirements_is_unaffected(repo):
    """The ~90% case: nothing extra required means nothing extra checked."""
    result = CarrierVerificationService(repo).verify(
        "MC123456", _load(repo, required_classifications=()))
    assert result.action == VerificationAction.PROCEED


def test_a_carrier_without_a_required_classification_goes_to_a_human(repo):
    carrier = _carrier(repo, qualifications=())
    load = _load(repo, required_classifications=("Critical Cargo",))

    result = _service(carrier).verify("MC123456", load)
    assert result.action == VerificationAction.HUMAN_REVIEW
    assert result.reason == "qualification_not_met"
    # They ARE who they said they were — that part verified fine. Only the match
    # between this carrier and this freight failed.
    assert result.verified is True
    assert any("Critical Cargo" in flag for flag in result.risk_flags)


def test_a_carrier_holding_the_requirement_proceeds(repo):
    carrier = _carrier(repo, qualifications=("Critical Cargo",))
    load = _load(repo, required_classifications=("Critical Cargo",))
    assert _service(carrier).verify("MC123456", load).action == \
        VerificationAction.PROCEED


def test_every_requirement_has_to_be_met_not_just_one(repo):
    carrier = _carrier(repo, qualifications=("Critical Cargo",))
    load = _load(repo, required_classifications=("Critical Cargo",
                                                 "Temperature Controlled"))
    result = _service(carrier).verify("MC123456", load)
    assert result.action == VerificationAction.HUMAN_REVIEW
    assert any("Temperature Controlled" in f for f in result.risk_flags)


def test_highway_overrides_transport_pro_in_the_dangerous_direction(repo):
    """Transport Pro claims the carrier holds Critical Cargo; Highway says they
    fail. Trusting the list here puts unqualified freight on a truck — this is the
    over-reporting case that made Highway authoritative in the first place."""
    carrier = _carrier(repo, qualifications=("Critical Cargo",),
                       highway_assessment=(("Critical Cargo", "fail"),))
    load = _load(repo, required_classifications=("Critical Cargo",))

    result = _service(carrier).verify("MC123456", load)
    assert result.action == VerificationAction.HUMAN_REVIEW
    assert result.reason == "qualification_not_met"


def test_highway_clears_a_carrier_transport_pro_omitted(repo):
    """The under-reporting case: Highway passes them, Transport Pro's list is
    stale. Declining here would turn our own data lag into a lost load."""
    carrier = _carrier(repo, qualifications=(),
                       highway_assessment=(("Critical Cargo", "pass"),))
    load = _load(repo, required_classifications=("Critical Cargo",))
    assert _service(carrier).verify("MC123456", load).action == \
        VerificationAction.PROCEED


# --------------------------------------------------------------------------- #
# Declared value vs cargo cover
# --------------------------------------------------------------------------- #
def test_freight_worth_more_than_the_cargo_cover_goes_to_a_human(repo):
    carrier = _carrier(repo, cargo_insurance_limit=100000.0)
    load = _load(repo, commodity_value=250000.0)

    result = _service(carrier).verify("MC123456", load)
    assert result.action == VerificationAction.HUMAN_REVIEW
    assert result.reason == "commodity_value_over_cover"


def test_freight_within_the_cargo_cover_proceeds(repo):
    carrier = _carrier(repo, cargo_insurance_limit=100000.0)
    assert _service(carrier).verify(
        "MC123456", _load(repo, commodity_value=50000.0)
    ).action == VerificationAction.PROCEED


def test_an_unreadable_cargo_limit_skips_the_check_rather_than_blocking(repo):
    """A carrier whose policy we failed to parse is not a carrier without
    insurance. Blocking here declines legitimate carriers on our own bug."""
    carrier = _carrier(repo, cargo_insurance_limit=None)
    assert _service(carrier).verify(
        "MC123456", _load(repo, commodity_value=250000.0)
    ).action == VerificationAction.PROCEED


def test_a_load_declaring_no_value_skips_the_check(repo):
    carrier = _carrier(repo, cargo_insurance_limit=1000.0)
    assert _service(carrier).verify(
        "MC123456", _load(repo, commodity_value=None)
    ).action == VerificationAction.PROCEED


# --------------------------------------------------------------------------- #
# Ordering, and the no-load case
# --------------------------------------------------------------------------- #
def test_verifying_without_a_load_still_works(repo):
    """The desk gate stands alone; the load-specific gates simply don't run."""
    assert CarrierVerificationService(repo).verify("MC123456").action == \
        VerificationAction.PROCEED


def test_a_suspended_carrier_is_labelled_by_the_suspension_not_the_load(repo):
    """A revoked carrier's problem is not Critical Cargo. Labelling it that way
    would be wrong, and more informative about our checks than we should be."""
    load = _load(repo, required_classifications=("Critical Cargo",),
                 commodity_value=999999.0)
    result = CarrierVerificationService(repo).verify("MC999888", load)
    assert result.action == VerificationAction.DECLINE
    assert result.reason == "authority_not_active"


# --------------------------------------------------------------------------- #
# PASS: vetted, but no agreement with us
# --------------------------------------------------------------------------- #
def test_pass_is_not_read_as_active():
    """Transport Pro's PASS means "cleared the rules, has not connected". Folding
    it into ACTIVE means booking a carrier we have no signed relationship with."""
    assert AuthorityStatus("PASS") is AuthorityStatus.NOT_CONNECTED
    assert AuthorityStatus("PASS").can_haul is False
    assert AuthorityStatus("PASS").is_definite is False
    # The genuinely-active spellings still land on ACTIVE.
    for spelling in ("ACTIVE", "Active", "authorized", "approved"):
        assert AuthorityStatus(spelling) is AuthorityStatus.ACTIVE, spelling


def test_a_not_connected_carrier_gets_an_invite_and_a_handoff(repo):
    carrier = _carrier(repo, authority_status=AuthorityStatus.NOT_CONNECTED,
                       raw_authority_status="PASS")
    result = _service(carrier).verify("MC123456")

    assert result.action == VerificationAction.HUMAN_REVIEW
    assert result.reason == "onboarding_not_connected"
    assert result.invite_to_onboard is True


def test_a_carrier_who_failed_vetting_is_never_invited_to_onboard(repo):
    """Inviting a carrier who FAILED would be an invitation to nothing."""
    for number in ("MC999888", "MC555444"):
        assert CarrierVerificationService(repo).verify(
            number).invite_to_onboard is False, number


def test_review_and_not_connected_are_told_apart(repo):
    """Both are non-definite and both go to a human, but only one has a remedy the
    agent can act on — so only one sets the invite flag."""
    carrier = _carrier(repo, authority_status=AuthorityStatus.PENDING,
                       raw_authority_status="REVIEW")
    result = _service(carrier).verify("MC123456")
    assert result.reason == "authority_pending_review"
    assert result.invite_to_onboard is False


# --------------------------------------------------------------------------- #
# Highway's verdict on the whole carrier
#
# From a live call: MC 1798414 is absent from `/voiceai/carrier_status`, reads FAIL
# on the HappyRobot endpoint, and fails every Highway classification with
# `needs_to_connect_eld`. Before this gate it ended in "let me get you to a rep".
# It should end in "your company doesn't currently meet the requirements".
# --------------------------------------------------------------------------- #
def test_a_highway_overall_fail_is_a_decline(repo):
    carrier = _carrier(repo, highway_overall_result="fail")
    result = _service(carrier).verify("MC123456")

    assert result.action == VerificationAction.DECLINE
    assert result.reason == "authority_not_active"
    assert result.verified is False
    assert "highway_overall_fail" in result.risk_flags


def test_a_highway_overall_fail_overrides_an_active_source_status(repo):
    """The commercially important direction. The seeded carrier is ACTIVE in the
    source system; Highway saying the carrier fails its rules outright has to win,
    or we book freight onto a carrier Highway has already refused."""
    base = _carrier(repo)
    assert base.authority_status is AuthorityStatus.ACTIVE     # the source says yes

    result = _service(_carrier(repo, highway_overall_result="fail")).verify("MC123456")
    assert result.action == VerificationAction.DECLINE


def test_it_also_beats_the_load_gates_to_the_answer(repo):
    """Labelled by the carrier's own failure, not by a classification the load
    happens to want — the carrier is not eligible for anything."""
    carrier = _carrier(repo, highway_overall_result="fail", qualifications=())
    load = _load(repo, required_classifications=("Critical Cargo",))
    assert _service(carrier).verify("MC123456", load).reason == "authority_not_active"


def test_pass_review_and_absent_all_fall_through(repo):
    """Only an explicit fail decides anything. Everything else defers to the source
    system, so an unreachable Highway changes nothing about who gets cleared."""
    for verdict in ("pass", "review", None):
        result = _service(_carrier(repo, highway_overall_result=verdict)
                          ).verify("MC123456")
        assert result.action == VerificationAction.PROCEED, verdict


def test_a_declined_carrier_is_never_invited_to_onboard(repo):
    """An invite to connect would be an invitation to nothing."""
    result = _service(_carrier(repo, highway_overall_result="fail")).verify("MC123456")
    assert result.invite_to_onboard is False
