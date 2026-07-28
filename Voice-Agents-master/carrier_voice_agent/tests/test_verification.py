from lanevoice.domain.models import VerificationAction
from lanevoice.services import CarrierVerificationService


def test_active_insured_carrier_proceeds(repo):
    result = CarrierVerificationService(repo).verify("MC123456")
    assert result.verified is True
    assert result.action == VerificationAction.PROCEED
    assert result.high_risk is False


def test_suspended_carrier_is_declined_not_reviewed(repo):
    """Authority that is not ACTIVE is a definite no from the source system, so
    the carrier is told they don't meet the requirements rather than being put in
    a review queue they'll never hear back from."""
    result = CarrierVerificationService(repo).verify("MC999888")
    assert result.verified is False
    assert result.action == VerificationAction.DECLINE
    assert result.reason == "authority_not_active"


def test_inactive_carrier_is_also_declined(repo):
    result = CarrierVerificationService(repo).verify("MC555444")
    assert result.action == VerificationAction.DECLINE
    assert result.reason == "authority_not_active"


def test_a_status_we_cannot_read_goes_to_a_human_not_a_decline(repo):
    """The distinction the whole three-outcome split exists for: an unreadable
    status is our mapping failing, not the carrier failing. Accusing a legitimate
    carrier of not meeting our requirements because we couldn't find a field is a
    worse error than making a rep look at it."""
    import dataclasses

    carrier = repo.get_carrier("MC123456")          # active, insured, approved
    unreadable = dataclasses.replace(carrier, raw_authority_status=None)
    assert unreadable.authority_reported is False

    service = CarrierVerificationService(repo)
    service._repo = type("_R", (), {"get_carrier": lambda _s, _n: unreadable})()
    result = service.verify("MC123456")
    assert result.action == VerificationAction.HUMAN_REVIEW
    assert result.reason == "authority_not_reported"
    assert "authority_not_reported" in result.risk_flags


def test_recently_reactivated_is_flagged_but_verified(repo):
    result = CarrierVerificationService(repo).verify("MC777111")
    assert result.verified is True
    assert result.high_risk is True
    assert result.action == VerificationAction.HUMAN_REVIEW
    assert "recently_reactivated" in result.risk_flags


def test_unknown_carrier_not_found(repo):
    result = CarrierVerificationService(repo).verify("MC000000")
    assert result.verified is False
    assert result.reason == "not_found"


def test_not_approved_carrier_is_declined(repo):
    result = CarrierVerificationService(repo).verify("MC222333")
    assert result.verified is True          # authority/insurance are fine
    assert result.approved is False         # but not approved to work with us
    assert result.action == VerificationAction.DECLINE
