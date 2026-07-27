from lanevoice.domain.models import VerificationAction
from lanevoice.services import CarrierVerificationService


def test_active_insured_carrier_proceeds(repo):
    result = CarrierVerificationService(repo).verify("MC123456")
    assert result.verified is True
    assert result.action == VerificationAction.PROCEED
    assert result.high_risk is False


def test_revoked_carrier_is_blocked(repo):
    result = CarrierVerificationService(repo).verify("MC999888")
    assert result.verified is False
    assert result.action == VerificationAction.HUMAN_REVIEW


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
