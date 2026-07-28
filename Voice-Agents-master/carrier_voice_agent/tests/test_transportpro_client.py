"""
Transport Pro HTTP client: auth, token refresh, retries and wire format.

Driven through an `httpx.MockTransport`, so the real client code runs — including
the bits that are easy to get wrong and impossible to notice until a live call
fails: Basic auth on login with no body, the bearer token on everything else,
one retry on a 401, and the collection's three-n `connnectionRecordType`.
"""


import httpx
import pytest

from lanevoice.integrations.transportpro.client import (
    TransportProAuthError,
    TransportProClient,
    TransportProError,
)
from tests.transportpro_fake import FakeTransportPro as _Server
from tests.transportpro_fake import client as _client
from tests.transportpro_fake import settings as _settings
from tests.transportpro_payloads import (
    CARRIER_STATUS_ACTIVE,
    CONTACT_SEARCH,
    EMPTY_SEARCH,
    SEARCH_AVAILABLE,
)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_login_uses_basic_auth_with_no_body_then_bearer_for_the_call():
    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(200, json=CARRIER_STATUS_ACTIVE))
    with _client(server) as client:
        assert client.carrier_status(mc_number="123456")["mc_number"] == "123456"

    login = server.calls("/auth")[0]
    assert login.method == "POST"
    assert not login.content                     # no body: that is the wire format
    assert login.headers["Authorization"].startswith("Basic ")
    assert server.bearers("/voiceai/carrier_status") == ["Bearer tok-1"]


def test_the_token_is_reused_rather_than_re_logging_in():
    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(200, json=CARRIER_STATUS_ACTIVE))
    with _client(server) as client:
        client.carrier_status(mc_number="1")
        client.carrier_status(mc_number="2")
    assert server.logins == 1
    assert len(server.calls("/voiceai/carrier_status")) == 2


def test_a_401_mid_call_refreshes_and_retries_once():
    """A token that ages out during a call must not cost the caller an answer."""
    server = _Server().on(
        "/voiceai/carrier_status",
        httpx.Response(401, json={"error": "expired"}),
        httpx.Response(200, json=CARRIER_STATUS_ACTIVE),
    )
    with _client(server) as client:
        assert client.carrier_status(dot_number="1000001") is not None

    assert server.refreshes == 1
    assert server.logins == 1
    # Second attempt carried the refreshed token, not the dead one.
    assert server.bearers("/voiceai/carrier_status") == [
        "Bearer tok-1", "Bearer tok-1+refreshed"]


def test_a_rejected_refresh_token_falls_back_to_a_full_login():
    """The refresh token is accepted at login and refused when used — an expired
    session rather than an expired access token. One extra login, still one retry,
    and the caller still gets their answer."""
    server = _Server(reject_refresh=True).on(
        "/voiceai/carrier_status",
        httpx.Response(401, json={"error": "expired"}),
        httpx.Response(200, json=CARRIER_STATUS_ACTIVE),
    )
    with _client(server) as client:
        assert client.carrier_status(mc_number="123456") is not None
    assert server.refreshes == 1
    assert server.logins == 2      # refresh refused -> logged in again


def test_a_server_that_issues_no_refresh_token_just_logs_in_again():
    server = _Server(refresh=None).on(
        "/voiceai/carrier_status",
        httpx.Response(401, json={"error": "expired"}),
        httpx.Response(200, json=CARRIER_STATUS_ACTIVE),
    )
    with _client(server) as client:
        assert client.carrier_status(mc_number="123456") is not None
    assert server.refreshes == 0    # nothing to refresh with, so don't try
    assert server.logins == 2


def test_bad_credentials_raise_and_are_not_retried():
    server = _Server()
    with TransportProClient(
        _settings(transport_pro_password="wrong"), transport=server.transport()
    ) as client:
        with pytest.raises(TransportProAuthError, match="rejected the API credentials"):
            client.carrier_status(mc_number="123456")
    assert server.logins == 1       # no point trying the same password again


def test_missing_base_url_is_a_clear_configuration_error():
    with pytest.raises(TransportProError, match="TRANSPORT_PRO_URL is not set"):
        TransportProClient(_settings(transport_pro_url=""))


def test_missing_credentials_are_a_clear_configuration_error():
    server = _Server()
    with TransportProClient(
        _settings(transport_pro_username=""), transport=server.transport()
    ) as client:
        with pytest.raises(TransportProAuthError, match="USERNAME"):
            client.carrier_status(mc_number="1")


# --------------------------------------------------------------------------- #
# Retries and failures
# --------------------------------------------------------------------------- #
def test_a_500_is_retried_once_then_raises():
    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(500, text="boom"),
                          httpx.Response(500, text="boom"))
    with _client(server) as client:
        with pytest.raises(TransportProError):
            client.carrier_status(mc_number="1")
    assert len(server.calls("/voiceai/carrier_status")) == 2   # not more


def test_a_transient_500_that_recovers_returns_the_answer():
    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(503, text="unavailable"),
                          httpx.Response(200, json=CARRIER_STATUS_ACTIVE))
    with _client(server) as client:
        assert client.carrier_status(mc_number="123456") is not None


def test_a_timeout_is_retried_then_surfaced():
    def timeout(_request):
        raise httpx.ReadTimeout("too slow")

    server = _Server().on("/voiceai/carrier_status", timeout)
    with _client(server) as client:
        with pytest.raises(TransportProError, match="after 2 attempts"):
            client.carrier_status(mc_number="1")


def test_a_404_means_no_such_record_not_an_error():
    server = _Server()          # nothing routed -> 404
    with _client(server) as client:
        assert client.carrier_status(mc_number="000000") is None
        assert client.load_detail("999999") is None


def test_a_400_is_raised_rather_than_read_as_empty():
    server = _Server().on("/voiceai/load/search_available",
                          httpx.Response(400, text="bad filter"))
    with _client(server) as client:
        with pytest.raises(TransportProError, match="HTTP 400"):
            client.search_available_loads(load_id="1")


def test_non_json_is_not_mistaken_for_a_record():
    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(200, text="<html>maintenance</html>"))
    with _client(server) as client:
        assert client.carrier_status(mc_number="1") is None


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #
def test_response_envelopes_are_all_understood():
    """The collection shows results-enveloped, bare-array and bare-object shapes
    across these endpoints, and several have no example at all."""
    server = _Server()
    server.on("/voiceai/load/search_available",
              httpx.Response(200, json=SEARCH_AVAILABLE))
    with _client(server) as client:
        assert len(client.search_available_loads(load_id="1303369")) == 1

    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(200, json=[CARRIER_STATUS_ACTIVE]))
    with _client(server) as client:
        assert client.carrier_status(mc_number="1")["carrier_id"] == 13167

    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(200, json=CARRIER_STATUS_ACTIVE))
    with _client(server) as client:
        assert client.carrier_status(mc_number="1")["carrier_id"] == 13167


def test_empty_results_are_an_empty_list_not_a_phantom_record():
    server = _Server().on("/voiceai/load/search_available",
                          httpx.Response(200, json=EMPTY_SEARCH))
    with _client(server) as client:
        assert client.search_available_loads(load_id="1") == []


def test_blank_query_parameters_are_dropped():
    """`carrier_status` takes mc_number OR dot_number. Sending `mc_number=` empty
    alongside a real dot_number is how you get an empty result set."""
    server = _Server().on("/voiceai/carrier_status",
                          httpx.Response(200, json=CARRIER_STATUS_ACTIVE))
    with _client(server) as client:
        client.carrier_status(dot_number="2999221")
    url = server.calls("/voiceai/carrier_status")[0].url
    assert url.params["dot_number"] == "2999221"
    assert "mc_number" not in url.params


def test_carrier_status_needs_at_least_one_identifier():
    server = _Server()
    with _client(server) as client:
        with pytest.raises(ValueError, match="mc_number or a dot_number"):
            client.carrier_status()


def test_contact_search_sends_the_apis_own_three_n_spelling():
    """`connnectionRecordType` is the wire format, typo and all. Spelling it
    correctly returns nothing, and every carrier then looks like they have no
    address on file — which would fail the booking gate for everybody."""
    server = _Server().on("/contact/search", httpx.Response(200, json=CONTACT_SEARCH))
    with _client(server) as client:
        assert len(client.carrier_contacts("13167")) == 2

    params = server.calls("/contact/search")[0].url.params
    assert params["connnectionRecordType"] == "brokerCarrier"   # three n's
    assert "connectionRecordType" not in params
    assert params["connectionRecordId"] == "13167"


def test_make_offer_posts_the_documented_form_fields():
    server = _Server().on("/voiceai/load/1302643/make_offer",
                          httpx.Response(200, json={"offer_id": 299}))
    with _client(server) as client:
        client.make_offer(
            "1302643",
            carrier_name="ABC Carrier", contact_name="Kenneth",
            offer_amount=475.0, email="test@domain.com", mc_number="343195",
            notes="booked by voice ai",
        )
    request = server.calls("/make_offer")[0]
    body = httpx.QueryParams(request.content.decode())
    assert body["carrier_name"] == "ABC Carrier"
    assert body["contact_name"] == "Kenneth"
    assert body["offer_amount"] == "475"         # whole dollars, not 475.0
    assert body["email"] == "test@domain.com"
    assert body["mc_number"] == "343195"
    assert body["notes"] == "booked by voice ai"
    assert "phone_number" not in body            # empty optionals are dropped


def test_make_offer_refuses_without_a_way_to_reach_the_carrier():
    server = _Server()
    with _client(server) as client:
        with pytest.raises(ValueError, match="email or a phone_number"):
            client.make_offer("1", carrier_name="A", contact_name="B",
                              offer_amount=100)


def test_add_load_note_posts_the_content_field():
    server = _Server().on("/voiceai/load/1303370/add_note",
                          httpx.Response(200, json={}))
    with _client(server) as client:
        client.add_load_note("1303370", "Add a test note from AI")
    body = httpx.QueryParams(server.calls("/add_note")[0].content.decode())
    assert body["content"] == "Add a test note from AI"


def test_add_carrier_capacity_posts_every_required_field():
    server = _Server().on("/voiceai/add_carrier_capacity",
                          httpx.Response(200, json={}))
    with _client(server) as client:
        client.add_carrier_capacity(
            carrier_name="ABC Carrier", contact_name="Kenneth",
            equipment_type="Flatbed", origin_city="Nashville",
            origin_state="TN", date_available="2025-04-30",
            phone_number="615-823-1937",
        )
    body = httpx.QueryParams(server.calls("/add_carrier_capacity")[0].content.decode())
    for field in ("carrier_name", "contact_name", "equipment_type",
                  "origin_city", "origin_state", "date_available"):
        assert body[field]
    assert body["phone_number"] == "615-823-1937"
