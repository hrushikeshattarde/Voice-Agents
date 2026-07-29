"""
Transport Pro Public API client (Circle Logistics tenant).

Thin, synchronous and deliberately boring: one method per endpoint the call flow
actually needs, no ORM, no response objects. The interesting logic lives in
`mappers.py` (API JSON -> domain models) and `repository.py` (the seam the
conversation layer talks to).

Endpoints used, all from the collection's `Voice AI` folder unless noted:

    POST /auth                                  login (HTTP Basic) / refresh
    GET  /voiceai/carrier_status                is this MC/DOT active with us?
    GET  /voiceai/load/search_available         posted, bookable loads
    GET  /voiceai/load/{id}                     load detail (any status)
    POST /voiceai/load/{id}/make_offer          record the agreed rate
    POST /voiceai/load/{id}/add_note            write the call outcome back
    POST /voiceai/add_carrier_capacity          the empty call, as capacity
    GET  /contact/search                        addresses on the carrier's file
    GET  /user/{id}                             the rep a load is assigned to

Two things about this API are load-bearing and easy to "fix" by accident:

* `POST /auth` takes HTTP Basic credentials and NO body. The refresh call to the
  same path takes a JSON body and no Basic header.
* `/contact/search` really does spell its parameter `connnectionRecordType`, with
  three n's. That typo is the wire format; correcting it returns nothing.

Everything is synchronous because the conversation layer is synchronous and the
worker already runs it in a thread (`asyncio.to_thread`). A single client
instance is shared across concurrent calls, so token refresh is locked.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings

logger = get_logger(__name__)

# Status codes worth trying again: the token may have aged out mid-call, or the
# far end hiccuped. A 4xx that isn't 401 will fail again identically.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class TransportProError(RuntimeError):
    """A Transport Pro call failed in a way the caller has to deal with."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class TransportProAuthError(TransportProError):
    """Credentials were rejected. Retrying will not help."""


class TransportProClient:
    """Authenticated access to the Transport Pro Public API.

    `transport` is an httpx transport, injected by the tests so the whole client
    (auth, refresh, retry, form encoding) is exercised without a network.
    """

    def __init__(self, settings: Settings, *, transport: Any | None = None):
        base = settings.transport_pro_url.rstrip("/")
        if not base:
            raise TransportProError(
                "TRANSPORT_PRO_URL is not set. Fill it in .env (see .env.example) "
                "or set DATA_SOURCE=sqlite to run against the offline seed data."
            )
        self._settings = settings
        self._base = base
        self._username = settings.transport_pro_username
        self._password = settings.transport_pro_password
        self._client = httpx.Client(
            base_url=base,
            timeout=httpx.Timeout(settings.transport_pro_timeout),
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TransportProClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- authentication ----------------------------------------------------- #
    def _store_tokens(self, payload: dict) -> None:
        token = payload.get("access_token")
        if not token:
            raise TransportProAuthError(
                "POST /auth succeeded but returned no access_token "
                f"(keys: {sorted(payload)})"
            )
        self._access_token = token
        self._refresh_token = payload.get("refresh_token") or self._refresh_token
        # Refresh a minute early when the API tells us the lifetime; when it
        # doesn't, lean on the 401-and-retry path instead of guessing a TTL.
        expires_in = payload.get("expires_in")
        try:
            self._expires_at = time.monotonic() + float(expires_in) - 60 if expires_in else 0.0
        except (TypeError, ValueError):
            self._expires_at = 0.0

    def _login(self) -> None:
        """HTTP Basic -> bearer tokens. No request body: that is the wire format."""
        if not self._username or not self._password:
            raise TransportProAuthError(
                "TRANSPORT_PRO_USERNAME / TRANSPORT_PRO_PASSWORD are not set."
            )
        response = self._client.post(
            "/auth", auth=(self._username, self._password)
        )
        if response.status_code in (401, 403):
            raise TransportProAuthError(
                f"Transport Pro rejected the API credentials for "
                f"{self._username!r} (HTTP {response.status_code}).",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise TransportProError(
                f"POST /auth failed with HTTP {response.status_code}: "
                f"{response.text[:200]}",
                status=response.status_code,
            )
        self._store_tokens(_json_object(response))
        logger.info("Transport Pro: authenticated as %s", self._username)

    def _refresh(self) -> bool:
        """Trade the refresh token for a new access token. False if we can't."""
        if not self._refresh_token:
            return False
        response = self._client.post(
            "/auth",
            json={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
        )
        if response.status_code >= 400:
            logger.info("Transport Pro: refresh token rejected (HTTP %s); "
                        "logging in again", response.status_code)
            self._refresh_token = None
            return False
        self._store_tokens(_json_object(response))
        logger.debug("Transport Pro: access token refreshed")
        return True

    def _authorize(self) -> str:
        with self._lock:
            if not self._access_token or (
                self._expires_at and time.monotonic() >= self._expires_at
            ):
                if not self._refresh():
                    self._login()
            return self._access_token or ""

    def _reauthorize(self) -> None:
        """Called after a 401: the token we just used is dead."""
        with self._lock:
            self._access_token = None
            if not self._refresh():
                self._login()

    # -- request plumbing --------------------------------------------------- #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
    ) -> Any:
        """One authenticated call, with a single retry for 401s and blips.

        Retries are capped at one extra attempt on purpose. This runs while a
        carrier is holding the line, so a second and third round trip against a
        struggling endpoint costs more (in dead air) than it can possibly win.
        """
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        body = {k: str(v) for k, v in (data or {}).items() if v not in (None, "")}
        last_error: Exception | None = None

        for attempt in (1, 2):
            token = self._authorize()
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params or None,
                    # The Voice AI writes are multipart/form-data in the
                    # collection; httpx's `data=` gives urlencoded form, which
                    # these endpoints accept and which keeps bodies loggable.
                    data=body or None,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("Transport Pro %s %s failed (attempt %d/2): %s",
                               method, path, attempt, exc)
                continue

            if response.status_code == 401 and attempt == 1:
                logger.info("Transport Pro: token rejected on %s %s; re-authenticating",
                            method, path)
                self._reauthorize()
                continue
            if response.status_code == 404:
                return None
            if response.status_code in _RETRY_STATUS and attempt == 1:
                last_error = TransportProError(
                    f"{method} {path} -> HTTP {response.status_code}",
                    status=response.status_code)
                logger.warning("Transport Pro %s %s -> HTTP %s; retrying",
                               method, path, response.status_code)
                continue
            if response.status_code >= 400:
                raise TransportProError(
                    f"{method} {path} -> HTTP {response.status_code}: "
                    f"{response.text[:200]}",
                    status=response.status_code,
                )
            return _json_or_none(response)

        raise TransportProError(
            f"{method} {path} failed after 2 attempts: {last_error}"
        ) from last_error

    # -- Voice AI: carrier vetting ------------------------------------------ #
    def carrier_status(
        self, *, mc_number: str | None = None, dot_number: str | None = None
    ) -> dict | None:
        """`GET /voiceai/carrier_status`. One of mc_number / dot_number required.

        Returns the raw record (unwrapped if the API nests it under `results`),
        or None when Transport Pro has no such carrier.
        """
        if not (mc_number or dot_number):
            raise ValueError("carrier_status needs an mc_number or a dot_number")
        payload = self._request(
            "GET",
            "/voiceai/carrier_status",
            params={"mc_number": mc_number, "dot_number": dot_number},
        )
        return _first_record(payload)

    # -- Voice AI: loads ---------------------------------------------------- #
    def load_detail(self, load_id: str) -> dict | None:
        """`GET /load/{id}` — the load behind a number the caller read out.

        This is the load lookup. The payload carries both sellability conditions
        outright — `status.loadStatus` and `postingInfo.isPosted` — along with the
        rates under `postingInfo`, so one call answers "is this real, is it
        sellable, and what may I open at". Returns None for a load that does not
        exist.
        """
        return _first_record(self._request("GET", f"/load/{load_id}"))

    def search_loads(
        self,
        *,
        pickup_date_start: str | None = None,
        pickup_date_end: str | None = None,
        load_status: str | None = None,
        is_posted: bool | None = None,
        equipment_type: str | None = None,
    ) -> list[dict]:
        """`GET /load/search` — the board, filtered.

        `loadStatus` and `isPosted` are real filters here, so the desk's two
        conditions are applied server-side rather than only checked on the way
        back. They are still re-checked on each record: a search endpoint that
        doesn't recognise a filter tends to ignore it rather than reject it.
        """
        payload = self._request(
            "GET",
            "/load/search",
            params={
                "pickupDateStart": pickup_date_start,
                "pickupDateEnd": pickup_date_end,
                "loadStatus": load_status,
                "isPosted": None if is_posted is None else str(is_posted).lower(),
                "equipmentType": equipment_type,
            },
        )
        return _records(payload)

    def search_available_loads(
        self,
        *,
        load_id: str | None = None,
        origin_state: str | None = None,
        origin_city: str | None = None,
        equipment_type: str | None = None,
    ) -> list[dict]:
        """`GET /voiceai/load/search_available` — the Voice AI posted-board query.

        Not on the call path: `load_detail` and `search_loads` are what the
        repository uses, because they speak the same payload shape and expose
        `loadStatus` / `isPosted` directly. Kept because it is a real endpoint and
        the only one that returns `carrier_sales_data.book_now_url`.
        """
        payload = self._request(
            "GET",
            "/voiceai/load/search_available",
            params={
                "load_id": load_id,
                "origin_state": origin_state,
                "origin_city": origin_city,
                "equipment_type": equipment_type,
            },
        )
        return _records(payload)

    def make_offer(
        self,
        load_id: str,
        *,
        carrier_name: str,
        contact_name: str,
        offer_amount: float | int,
        email: str | None = None,
        phone_number: str | None = None,
        mc_number: str | None = None,
        dot_number: str | None = None,
        carrier_id: str | None = None,
        notes: str | None = None,
    ) -> dict | None:
        """`POST /voiceai/load/{id}/make_offer` — the agreed rate, on the record.

        `email` or `phone_number` is required by the API; the booking flow always
        has a verified address by the time it gets here.
        """
        if not (email or phone_number):
            raise ValueError("make_offer needs an email or a phone_number")
        payload = self._request(
            "POST",
            f"/voiceai/load/{load_id}/make_offer",
            data={
                "carrier_name": carrier_name,
                "contact_name": contact_name,
                "offer_amount": int(round(float(offer_amount))),
                "email": email,
                "phone_number": phone_number,
                "mc_number": mc_number,
                "dot_number": dot_number,
                "carrier_id": carrier_id,
                "notes": notes,
            },
        )
        return payload if isinstance(payload, dict) else None

    def add_load_note(self, load_id: str, content: str) -> None:
        """`POST /voiceai/load/{id}/add_note`."""
        self._request("POST", f"/voiceai/load/{load_id}/add_note",
                      data={"content": content})

    def add_carrier_capacity(
        self,
        *,
        carrier_name: str,
        contact_name: str,
        equipment_type: str,
        origin_city: str,
        origin_state: str,
        date_available: str,
        email: str | None = None,
        phone_number: str | None = None,
        mc_number: str | None = None,
        dot_number: str | None = None,
        carrier_id: str | None = None,
        notes: str | None = None,
    ) -> None:
        """`POST /voiceai/add_carrier_capacity` — the empty call as a capacity row.

        Every field above the optionals is required by the API, so the caller
        checks it has them before spending the round trip.
        """
        if not (email or phone_number):
            raise ValueError("add_carrier_capacity needs an email or a phone_number")
        self._request(
            "POST",
            "/voiceai/add_carrier_capacity",
            data={
                "carrier_name": carrier_name,
                "contact_name": contact_name,
                "equipment_type": equipment_type,
                "origin_city": origin_city,
                "origin_state": origin_state,
                "date_available": date_available,
                "email": email,
                "phone_number": phone_number,
                "mc_number": mc_number,
                "dot_number": dot_number,
                "carrier_id": carrier_id,
                "notes": notes,
            },
        )

    # -- Contacts: the addresses on a carrier's file ------------------------ #
    def carrier_contacts(self, carrier_id: str) -> list[dict]:
        """`GET /contact/search` for one broker-carrier record.

        NOTE the parameter name: `connnectionRecordType`, three n's. That is the
        API's spelling, not a typo in this file — spelling it correctly returns
        an empty set and every carrier looks like it has no address on file.
        """
        payload = self._request(
            "GET",
            "/contact/search",
            params={
                "connnectionRecordType": "brokerCarrier",
                "connectionRecordId": carrier_id,
            },
        )
        return _records(payload)

    # -- Users: the people a load is assigned to ---------------------------- #
    def user(self, user_id: str | int) -> dict | None:
        """`GET /user/{id}` — the rep behind an id in a load's `internalContacts`.

        A load names its people as bare ids (`{"type": "CARRIERREP", "id": 2423}`),
        so this is the second half of "who does this load belong to": the id comes
        off the load, the name and the phone number come from here. Returns None
        for an id Transport Pro doesn't know.
        """
        return _first_record(self._request("GET", f"/user/{user_id}"))


# --------------------------------------------------------------------------- #
# Response shape helpers
#
# The collection shows three shapes across these endpoints: a bare object, a
# bare array, and `{"pagination": {...}, "results": [...]}`. Several endpoints
# have no saved example at all, so every read goes through these rather than
# assuming one of them.
# --------------------------------------------------------------------------- #
def _json_or_none(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning("Transport Pro returned non-JSON for %s: %r",
                       response.request.url, response.text[:200])
        return None


def _json_object(response: httpx.Response) -> dict:
    payload = _json_or_none(response)
    if not isinstance(payload, dict):
        raise TransportProAuthError(
            f"POST /auth returned {type(payload).__name__}, expected an object")
    return payload


# Keys that wrap the actual records. `results` is the paginated envelope;
# `carrier_record` is what `/voiceai/carrier_status` uses — it returns
# `{"carrier_record": [ ... ], "carrier_onboarding_team": { ... }}`, and without
# unwrapping it the onboarding team's fields sit at the same depth as the
# carrier's, which is how a contact phone ends up read as a carrier attribute.
_ENVELOPE_KEYS = ("results", "carrier_record")


def _records(payload: Any) -> list[dict]:
    """Every record in a response, whatever envelope it arrived in."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in _ENVELOPE_KEYS:
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, dict):
                return [inner]
        return [payload]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def _first_record(payload: Any) -> dict | None:
    records = _records(payload)
    return records[0] if records else None
