"""
The booking link: offer -> accept -> URL, and the honesty that depends on it.

`make_offer` logs a rate for a rep. It is NOT a booking, and for a long time this
codebase asserted in a docstring that no accept step existed — so a call could end
with the agent telling a carrier they were booked when nothing bookable had
happened. These tests pin the real three-step relationship:

    POST /offer   -> offer_id      the rate is on the record
    accept_offer  -> book_now_url  the carrier can now sign
    (the carrier opens the link)   the load is finally theirs

The failure modes matter more than the happy path, because by the time any of this
runs the carrier has already agreed a rate and the call has to end in something
coherent regardless of what the TMS does.

Everything is driven through `httpx.MockTransport`. Nothing here touches the live
tenant: `accept_offer` accepts a real offer on a real load and `invite_carrier`
emails a real carrier, so neither is ever fired from a test.
"""

import json

import httpx
import pytest

from lanevoice.db.repository import Repository
from lanevoice.domain.models import AuthorityStatus, Carrier, Load, LoadStatus
from lanevoice.integrations.transportpro.client import TransportProClient
from lanevoice.integrations.transportpro.happyrobot import (
    HappyRobotClient,
    HappyRobotError,
)
from lanevoice.integrations.transportpro.repository import TransportProRepository
from lanevoice.settings import get_settings

BOOK_URL = "https://cli.transportpro.net/book/abc123"


def _settings(**overrides):
    base = {
        "transport_pro_url": "https://tp.test/publicapi/v1",
        "transport_pro_username": "u",
        "transport_pro_password": "p",
        "happyrobot_url": "https://tp.test/svc/happyrobot.php",
        "happyrobot_token": "hr-token",
    }
    return get_settings().model_copy(update=base | overrides)


def _load(**overrides) -> Load:
    base = {
        "load_id": "2520571", "origin": "Sikeston, MO", "destination": "Atlanta, GA",
        "pickup_date": "2026-08-13", "equipment": "Reefer", "weight_lbs": 38309,
        "open_rate": 1600.0, "ceiling_rate": 2200.0, "fraud_low_rate": 800.0,
        "assigned_rep_id": None, "status": LoadStatus.OPEN,
    }
    return Load(**(base | overrides))


def _carrier(**overrides) -> Carrier:
    base = {
        "usdot_number": "4153083", "mc_number": "1594669",
        "legal_name": "Los Aguilares Transportation",
        "authority_status": AuthorityStatus.ACTIVE, "insurance_on_file": True,
        "raw_authority_status": "ACTIVE", "carrier_id": "154887",
    }
    return Carrier(**(base | overrides))


class _Fake:
    """Routes /auth, /offer and happyrobot.php, recording every request body."""

    def __init__(self, *, offer_id="99001", accept=None, offer_status="SUCCESS"):
        self.offer_id = offer_id
        self.accept = accept if accept is not None else {"book_now_url": BOOK_URL}
        self.offer_status = offer_status
        self.calls: list[tuple[str, dict]] = []

    def _handle(self, request):
        path = request.url.path
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {"_raw": request.content.decode(errors="replace")}
        self.calls.append((path, body))

        if path.endswith("/auth"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path.endswith("/offer"):
            if self.offer_status != "SUCCESS":
                return httpx.Response(200, json={"STATUS": self.offer_status})
            return httpx.Response(200, json={"STATUS": "SUCCESS",
                                             "result": {"id": self.offer_id}})
        if "happyrobot" in path:
            if isinstance(self.accept, int):
                return httpx.Response(self.accept, text="boom")
            return httpx.Response(200, json=self.accept)
        return httpx.Response(404)

    @property
    def actions(self) -> list[str]:
        return [b.get("action") for p, b in self.calls if "happyrobot" in p]

    def body_for(self, needle: str) -> dict:
        for path, body in self.calls:
            if needle in path:
                return body
        raise AssertionError(f"no request to {needle}: {[p for p, _ in self.calls]}")


def _repo(fake, tmp_path, *, with_hr=True, **overrides):
    settings = _settings(**overrides)
    transport = httpx.MockTransport(fake._handle)
    client = TransportProClient(settings, transport=transport)
    hr = HappyRobotClient(settings, transport=transport) if with_hr else None
    audit = Repository(_db(tmp_path))
    return TransportProRepository(client, audit, settings, happyrobot=hr)


def _db(tmp_path):
    from lanevoice.db import Database

    db = Database(str(tmp_path / "audit.db"))
    db.init(seed=False)
    db.seed_reps()
    return db


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_booking_produces_a_real_link(tmp_path):
    fake = _Fake()
    repo = _repo(fake, tmp_path)

    attempt = repo.booking_link(_load(), _carrier(), 2000.0,
                                email="dispatch@carrier.com", phone="6155550100")

    assert attempt.url == BOOK_URL
    assert attempt.link_issued is True
    assert attempt.offer_recorded is True
    assert attempt.offer_id == "99001"
    assert fake.actions == ["accept_offer"]


def test_the_offer_is_created_with_the_types_the_endpoint_demands(tmp_path):
    """`POST /offer` rejects `"loadId": "2520571"` where it accepts `2520571`, so
    this endpoint is JSON-with-real-ints, unlike the form-encoded /voiceai writes."""
    fake = _Fake()
    _repo(fake, tmp_path).booking_link(_load(), _carrier(), 2000.0,
                                       email="dispatch@carrier.com")

    body = fake.body_for("/offer")
    assert body["loadId"] == 2520571 and isinstance(body["loadId"], int)
    assert body["amount"] == 2000 and isinstance(body["amount"], int)
    assert body["brokerCarrierId"] == 154887
    assert body["carrierName"] == "Los Aguilares Transportation"
    assert body["mcNumber"] == "1594669"


def test_the_booking_path_is_attributed_to_the_booking_user(tmp_path):
    """4876 and 4236 are NOT interchangeable — send_offer rejects 4876 outright.
    Only the REST booking path may use it."""
    fake = _Fake()
    _repo(fake, tmp_path).booking_link(_load(), _carrier(), 2000.0,
                                       email="a@b.com")
    assert fake.body_for("/offer")["recordAsUserId"] == 4876


def test_a_blank_contact_field_is_sent_as_a_placeholder(tmp_path):
    """This endpoint rejects a blank required string with a code that doesn't say
    which field was empty. "." is the placeholder the desk already recognises."""
    fake = _Fake()
    _repo(fake, tmp_path).booking_link(_load(), _carrier(), 2000.0,
                                       email="a@b.com")   # no phone
    body = fake.body_for("/offer")
    assert body["phone"] == "."
    assert body["email"] == "a@b.com"


def test_the_accepted_offer_id_is_the_one_that_was_created(tmp_path):
    fake = _Fake(offer_id="12345")
    _repo(fake, tmp_path).booking_link(_load(), _carrier(), 2000.0, email="a@b.com")
    assert fake.body_for("happyrobot")["data"]["offer_id"] == 12345


def test_a_nested_book_now_url_is_still_found(tmp_path):
    """The envelope varies across this API — the URL has been seen at the top
    level, under `data`, and under a row's `carrier_sales_data`."""
    for payload in (
        {"data": {"book_now_url": BOOK_URL}},
        {"result": {"book_now_url": BOOK_URL}},
        {"data": [{"book_now_url": BOOK_URL}]},
        {"data": [{"carrier_sales_data": {"book_now_url": BOOK_URL}}]},
    ):
        fake = _Fake(accept=payload)
        attempt = _repo(fake, tmp_path).booking_link(_load(), _carrier(), 2000.0,
                                                     email="a@b.com")
        assert attempt.url == BOOK_URL, payload


# --------------------------------------------------------------------------- #
# Failure modes — each one still has to leave the call somewhere coherent
# --------------------------------------------------------------------------- #
def test_no_happyrobot_credentials_means_no_link_rather_than_a_crash(tmp_path):
    """A deployment without these credentials logs the rate for a rep. That is a
    reduced capability, not an error."""
    fake = _Fake()
    repo = _repo(fake, tmp_path, with_hr=False)
    attempt = repo.booking_link(_load(), _carrier(), 2000.0, email="a@b.com")
    assert attempt.link_issued is False
    # Nothing was recorded either, which is what lets the caller safely fall back
    # to logging the offer instead.
    assert attempt.offer_recorded is False
    assert not fake.calls          # nothing was even attempted


def test_a_failed_offer_creation_yields_no_link(tmp_path):
    fake = _Fake(offer_status="FAILURE")
    repo = _repo(fake, tmp_path)
    attempt = repo.booking_link(_load(), _carrier(), 2000.0, email="a@b.com")
    assert attempt.link_issued is False
    assert attempt.offer_recorded is False   # the lane is untouched
    assert fake.actions == []      # never tried to accept a nonexistent offer


def test_an_accept_failure_leaves_the_offer_on_the_record(tmp_path):
    """The offer EXISTS at this point. Returning None (not raising) is what lets
    the agent hand over gracefully, and the log has to say the rate was recorded or
    whoever reads it will double-book the lane."""
    fake = _Fake(accept=500)
    repo = _repo(fake, tmp_path)
    attempt = repo.booking_link(_load(), _carrier(), 2000.0, email="a@b.com")

    assert attempt.link_issued is False
    # THE DISTINCTION THAT MATTERS: the rate is on the load. A rep who assumes
    # otherwise creates a second offer against the same lane.
    assert attempt.offer_recorded is True
    assert attempt.offer_id == "99001"
    assert fake.body_for("/offer")["amount"] == 2000


def test_an_accept_that_returns_no_url_is_not_a_link(tmp_path):
    fake = _Fake(accept={"STATUS": "SUCCESS"})
    repo = _repo(fake, tmp_path)
    attempt = repo.booking_link(_load(), _carrier(), 2000.0, email="a@b.com")
    assert attempt.link_issued is False
    assert attempt.offer_recorded is True     # accepted, just no URL came back


def test_no_contact_details_means_no_offer_is_attempted(tmp_path):
    fake = _Fake()
    repo = _repo(fake, tmp_path)
    attempt = repo.booking_link(_load(), _carrier(), 2000.0)
    assert attempt.link_issued is False
    assert attempt.offer_recorded is False
    assert not fake.calls


def test_a_body_level_error_code_on_a_200_is_a_failure(tmp_path):
    """This endpoint reports failure in the BODY of a 200. A client that only
    checked the HTTP status would read every one of them as success."""
    fake = _Fake(accept={"response_code": 301, "message": "Offer NOT saved."})
    repo = _repo(fake, tmp_path)
    attempt = repo.booking_link(_load(), _carrier(), 2000.0, email="a@b.com")
    assert attempt.link_issued is False
    assert attempt.offer_recorded is True


# --------------------------------------------------------------------------- #
# The Highway invite
# --------------------------------------------------------------------------- #
def test_the_invite_goes_to_the_address_on_file(tmp_path, monkeypatch):
    """NEVER to an address heard on the call. An unverified address plus a
    broker-branded onboarding link is a phishing message aimed at a real carrier,
    and refusing to trust a spoken address is the whole point of the email gate."""
    fake = _Fake(accept={"STATUS": "SUCCESS"})
    repo = _repo(fake, tmp_path)
    monkeypatch.setattr(repo, "carrier_emails",
                        lambda _dot: ("onfile@carrier.com",))

    assert repo.invite_to_onboard(
        _carrier(authority_status=AuthorityStatus.NOT_CONNECTED)) is True
    body = fake.body_for("happyrobot")
    assert body["action"] == "invite_carrier"
    assert body["data"] == {"mc_number": "1594669", "email": "onfile@carrier.com"}


def test_no_address_on_file_means_no_invite(tmp_path, monkeypatch):
    fake = _Fake()
    repo = _repo(fake, tmp_path)
    monkeypatch.setattr(repo, "carrier_emails", lambda _dot: ())
    assert repo.invite_to_onboard(_carrier()) is False
    assert not fake.calls


def test_no_mc_number_means_no_invite(tmp_path, monkeypatch):
    """`invite_carrier` is keyed on MC; there is nothing to send without one."""
    fake = _Fake()
    repo = _repo(fake, tmp_path)
    monkeypatch.setattr(repo, "carrier_emails", lambda _dot: ("a@b.com",))
    assert repo.invite_to_onboard(_carrier(mc_number=None)) is False
    assert not fake.calls


def test_an_invite_failure_is_reported_rather_than_raised(tmp_path, monkeypatch):
    """A failed invite must not change what the caller hears."""
    fake = _Fake(accept=500)
    repo = _repo(fake, tmp_path)
    monkeypatch.setattr(repo, "carrier_emails", lambda _dot: ("a@b.com",))
    assert repo.invite_to_onboard(_carrier()) is False


def test_carrier_lookup_treats_no_match_as_a_real_answer(tmp_path):
    """MC 2493819 returns code 300 "no carriers matching" — that is "not on file",
    not a failure, and must not abort a call."""
    fake = _Fake(accept={"response_code": 300, "message": "no carriers matching.",
                         "data": None})
    settings = _settings()
    hr = HappyRobotClient(settings, transport=httpx.MockTransport(fake._handle))
    assert hr.carrier_lookup(mc="2493819") is None


def test_a_real_carrier_lookup_error_still_raises(tmp_path):
    fake = _Fake(accept={"response_code": 401, "message": "bad token"})
    settings = _settings()
    hr = HappyRobotClient(settings, transport=httpx.MockTransport(fake._handle))
    with pytest.raises(HappyRobotError, match="401"):
        hr.carrier_lookup(mc="1594669")
