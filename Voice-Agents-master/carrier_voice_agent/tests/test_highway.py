"""
Highway: the independent read on a carrier, and how its verdicts resolve.

Highway is here because Transport Pro's classification list has been observed
wrong IN BOTH DIRECTIONS. So the tests that matter most are the ones pinning that
Highway wins each way — and, just as important, that a Highway failure never
turns into a decline. An enrichment source that can reject live carriers when it
has an outage is worse than no enrichment source.

The payload shapes are the real ones, captured from MC 1594669 on 2026-08-12.
"""

import httpx
import pytest

from lanevoice.domain.models import AuthorityStatus, Carrier
from lanevoice.integrations.highway import mappers
from lanevoice.integrations.highway.client import HighwayClient, HighwayError
from lanevoice.settings import get_settings

# Trimmed from the live response for MC 1594669 (Los Aguilares Transportation).
HIGHWAY_RECORD = {
    "id": 444964,
    "dba_name": "LOS AGUILARES TRANSPORTATION",
    "dot_number": "04153083",
    "identifiers": [
        {"is_type": "DOT", "value": "04153083"},
        {"is_type": "MC", "value": "1594669"},
    ],
    "rules_assessment": {
        "classifications": [
            {"name": "Expeditors", "result": "pass"},
            {"name": "Interstate", "result": "pass"},
            {"name": "Temperature Controlled", "result": "fail"},
            {"name": "Critical Cargo", "result": "pass"},
            {"name": "Preferred Insurance", "result": "pass"},
        ]
    },
    "insurance": {
        "insurance_policies": [
            {"is_type": "auto_liability", "status": "active", "limit": "1000000.0"},
            # The live feed types the limit as a STRING, not a number.
            {"is_type": "motor_truck_cargo", "status": "active", "limit": "100000.0"},
            {"is_type": "motor_truck_cargo", "status": "expired", "limit": "250000.0"},
        ]
    },
}


def _settings(**overrides):
    base = {"highway_api_token": "test-token",
            "highway_api_url": "https://highway.test/carriers"}
    return get_settings().model_copy(update=base | overrides)


def _client(handler, **overrides):
    return HighwayClient(_settings(**overrides),
                         transport=httpx.MockTransport(handler))


def _carrier(**overrides) -> Carrier:
    base = {
        "usdot_number": "4153083",
        "mc_number": "1594669",
        "legal_name": "Los Aguilares Transportation",
        "authority_status": AuthorityStatus.ACTIVE,
        "insurance_on_file": True,
        "raw_authority_status": "ACTIVE",
    }
    return Carrier(**(base | overrides))


# --------------------------------------------------------------------------- #
# The mappers
# --------------------------------------------------------------------------- #
def test_classifications_become_name_result_pairs():
    assert mappers.classifications(HIGHWAY_RECORD) == (
        ("Expeditors", "pass"),
        ("Interstate", "pass"),
        ("Temperature Controlled", "fail"),
        ("Critical Cargo", "pass"),
        ("Preferred Insurance", "pass"),
    )


def test_an_unrecognised_result_is_dropped_not_guessed_at():
    """An absent classification means "fall back to Transport Pro", which is the
    safe reading of a verdict we don't understand. Guessing would either decline a
    good carrier or clear a bad one."""
    record = {"rules_assessment": {"classifications": [
        {"name": "Interstate", "result": "conditional"},
        {"name": "Critical Cargo", "result": "pass"},
    ]}}
    assert mappers.classifications(record) == (("Critical Cargo", "pass"),)


def test_a_missing_or_malformed_assessment_is_no_opinion():
    for record in ({}, None, "nope", {"rules_assessment": None},
                   {"rules_assessment": {"classifications": "no"}},
                   {"rules_assessment": {"classifications": [None, {}, {"name": ""}]}}):
        assert mappers.classifications(record) == (), record


def test_the_cargo_limit_is_the_highest_active_motor_truck_cargo_policy():
    """Active wins over expired, so the $250k expired policy must not be read as
    cover the carrier has."""
    assert mappers.cargo_insurance_limit(HIGHWAY_RECORD) == 100000.0


def test_cargo_policies_with_no_active_one_still_report_a_limit():
    """"We saw a $100k cargo policy" beats "we saw nothing" for a check whose only
    job is catching freight worth more than the cover. The status vocabulary here
    is undocumented, so an unfamiliar value must not zero out the limit."""
    record = {"insurance": {"insurance_policies": [
        {"is_type": "motor_truck_cargo", "status": "lapsed?", "limit": "75000"},
    ]}}
    assert mappers.cargo_insurance_limit(record) == 75000.0


def test_no_cargo_policy_reads_as_unknown_not_as_zero():
    """None makes the caller SKIP the value check. Zero would make every load look
    like it exceeds the carrier's cover."""
    for record in ({}, None, {"insurance": {}},
                   {"insurance": {"insurance_policies": []}},
                   {"insurance": {"insurance_policies": [
                       {"is_type": "auto_liability", "limit": "1000000"}]}},
                   {"insurance": {"insurance_policies": [
                       {"is_type": "motor_truck_cargo", "limit": "not a number"}]}}):
        assert mappers.cargo_insurance_limit(record) is None, record


def test_the_company_name_is_preferred_over_a_persons_name():
    """Transport Pro returns "Victor Hugo Vargas Aguilar" for this MC — a person.
    The agent is told to confirm carriers by COMPANY name."""
    assert mappers.company_name(HIGHWAY_RECORD) == "Los Aguilares Transportation"


def test_a_shouted_name_is_title_cased_for_the_voice():
    """All-caps reaches the TTS voice as a coin toss between a name and an
    initialism spelled out letter by letter."""
    assert mappers.company_name({"dba_name": "ACME TRUCKING LLC"}) == "Acme Trucking Llc"
    # Mixed case is left exactly as the feed wrote it.
    assert mappers.company_name({"dba_name": "McLane Transport"}) == "McLane Transport"


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
def test_leading_zeros_are_stripped_from_the_identifier():
    """Highway keys on the bare number: MC/0001594669 is a 404 where MC/1594669
    is a hit, and a caller's record legitimately carries the padded form."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=HIGHWAY_RECORD)

    _client(handler).carrier(mc="0001594669")
    assert seen[0].endswith("/MC/1594669/by_identifier")


def test_dot_is_used_when_no_mc_is_given():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=HIGHWAY_RECORD)

    _client(handler).carrier(dot="04153083")
    assert seen[0].endswith("/DOT/4153083/by_identifier")


def test_the_token_is_sent_as_a_bearer_header():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=HIGHWAY_RECORD)

    _client(handler).carrier(mc="1594669")
    assert seen[0] == "Bearer test-token"


def test_a_token_already_carrying_its_prefix_is_not_doubled():
    """The value is usually copied from somewhere that includes "Bearer ", and
    "Bearer Bearer ey..." is a 401 indistinguishable from an expired key."""
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=HIGHWAY_RECORD)

    _client(handler, highway_api_token="Bearer test-token").carrier(mc="1594669")
    assert seen[0] == "Bearer test-token"


def test_a_404_is_not_on_highway_rather_than_an_error():
    """Plenty of legitimate carriers on a broker's own books have no Highway
    record. Raising here would turn that into a failed call."""
    client = _client(lambda _r: httpx.Response(404, text="identifier not found"))
    assert client.carrier(mc="2493819") is None


def test_an_expired_token_says_so():
    """This credential is a JWT with a hard expiry, so "it worked last month" is
    the normal way it fails — the message has to point at that."""
    client = _client(lambda _r: httpx.Response(401, text="unauthorized"))
    with pytest.raises(HighwayError, match="hard expiry"):
        client.carrier(mc="1594669")


def test_a_lookup_with_neither_number_is_a_programming_error():
    client = _client(lambda _r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="mc or a dot"):
        client.carrier()


# --------------------------------------------------------------------------- #
# The resolution rule — the reason Highway is consulted at all
# --------------------------------------------------------------------------- #
def test_highway_pass_qualifies_a_carrier_transport_pro_omitted():
    """The under-reporting direction: Transport Pro's list is missing a
    qualification the carrier actually holds."""
    carrier = _carrier(qualifications=(),
                       highway_assessment=(("Critical Cargo", "pass"),))
    assert carrier.qualifies_for("Critical Cargo") is True


def test_highway_fail_disqualifies_a_carrier_transport_pro_claimed():
    """The over-reporting direction, which is the dangerous one: Transport Pro
    says the carrier holds it, Highway says they fail. Trusting the list here puts
    unqualified freight on a truck."""
    carrier = _carrier(qualifications=("Critical Cargo",),
                       highway_assessment=(("Critical Cargo", "fail"),))
    assert carrier.qualifies_for("Critical Cargo") is False


def test_review_defers_to_transport_pros_list():
    """Highway has no opinion, so the fallback decides — both ways."""
    assert _carrier(qualifications=("Critical Cargo",),
                    highway_assessment=(("Critical Cargo", "review"),)
                    ).qualifies_for("Critical Cargo") is True
    assert _carrier(qualifications=(),
                    highway_assessment=(("Critical Cargo", "review"),)
                    ).qualifies_for("Critical Cargo") is False


def test_no_highway_data_at_all_defers_to_transport_pros_list():
    """The outage path. Highway being unreachable must leave vetting exactly where
    it was before Highway existed."""
    assert _carrier(qualifications=("Critical Cargo",)
                    ).qualifies_for("Critical Cargo") is True
    assert _carrier(qualifications=()).qualifies_for("Critical Cargo") is False


def test_classification_names_match_case_insensitively():
    """The two systems disagree on capitalisation of the same classification often
    enough that an exact compare silently drops real qualifications."""
    carrier = _carrier(qualifications=("critical cargo",))
    assert carrier.qualifies_for("Critical Cargo") is True
    assert _carrier(highway_assessment=(("CRITICAL CARGO", "pass"),)
                    ).qualifies_for("Critical Cargo") is True


def test_an_unrelated_classification_is_not_confused_for_the_asked_one():
    carrier = _carrier(qualifications=("Interstate",),
                       highway_assessment=(("Interstate", "pass"),))
    assert carrier.qualifies_for("Critical Cargo") is False


# --------------------------------------------------------------------------- #
# `overall_result` — Highway's verdict on the whole carrier
#
# Coarser than the per-classification results and answering a different question:
# not "may they haul reefer" but "do they clear our rules at all". MC 1798414 is
# the worked example — every classification failing, overall "fail", and
# `needs_to_connect_eld` as the reason.
# --------------------------------------------------------------------------- #
DS35_RECORD = {
    "dba_name": "DS35 ENTERPRISES",
    "rules_assessment": {
        "overall_result": "fail",
        "summary": {"overall_result": "fail",
                    "carrier_actions_to_improve_rules_result": "needs_to_connect_eld"},
        "classifications": [
            {"name": "Interstate", "result": "fail"},
            {"name": "Critical Cargo", "result": "fail"},
            {"name": "Preferred Insurance", "result": "fail"},
        ],
    },
    "authority_assessment": {"rating": "Review Required",
                             "carrier_interstate_authority_check": "Inactive"},
}


def test_the_overall_verdict_is_read():
    assert mappers.overall_result(DS35_RECORD) == "fail"
    assert mappers.overall_result(HIGHWAY_RECORD) is None   # no overall_result key


def test_the_overall_verdict_is_none_when_highway_says_nothing():
    """None must read as "no opinion", never as a failure — otherwise an
    unreachable Highway starts declining live carriers."""
    for record in ({}, None, "nope", {"rules_assessment": None},
                   {"rules_assessment": {}},
                   {"rules_assessment": {"overall_result": None}},
                   {"rules_assessment": {"overall_result": "something new"}}):
        assert mappers.overall_result(record) is None, record


def test_pass_and_review_are_read_verbatim():
    for verdict in ("pass", "review", "fail"):
        record = {"rules_assessment": {"overall_result": verdict}}
        assert mappers.overall_result(record) == verdict
    # Case and whitespace are folded, since this is compared as a string.
    assert mappers.overall_result(
        {"rules_assessment": {"overall_result": "  FAIL "}}) == "fail"


def test_the_overall_verdict_is_independent_of_the_classifications():
    """A carrier can fail a classification and still clear the rules overall, so
    the two are read separately and mean different things."""
    record = {"rules_assessment": {
        "overall_result": "pass",
        "classifications": [{"name": "Temperature Controlled", "result": "fail"}],
    }}
    assert mappers.overall_result(record) == "pass"
    assert mappers.classifications(record) == (("Temperature Controlled", "fail"),)
