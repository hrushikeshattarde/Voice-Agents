"""
Highway Connect external API — the independent read on a carrier.

One endpoint, one method:

    GET /carriers/{MC|DOT}/{identifier}/by_identifier

Highway exists in this system because Transport Pro's classification list has
been observed WRONG IN BOTH DIRECTIONS on the live tenant — omitting a
qualification the carrier holds, and claiming one Highway says they fail. Neither
source is reliable alone, so the vetting gate reads both and lets Highway's
`rules_assessment` win where it has an opinion (`Carrier.qualifies_for`).

Three things it gives us that Transport Pro does not:

  * `rules_assessment.classifications` — pass/fail/review per classification,
    which is the authoritative qualification answer.
  * `insurance.insurance_policies` — the active `motor_truck_cargo` limit, which
    is the only way to check a load's declared value against real coverage.
  * `dba_name` — the trading name. Transport Pro's `carrier_name` is frequently a
    PERSON (an owner-operator's own name), and the agent is instructed to confirm
    carriers by company name, so this is what should be read back on the phone.

Synchronous, like every other client here, because the conversation layer is
synchronous and the worker runs it in a thread. Deliberately unauthenticated at
construction: there is no login step, just a long-lived bearer token.
"""

from __future__ import annotations

from typing import Any

import httpx

from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings

logger = get_logger(__name__)


class HighwayError(RuntimeError):
    """A Highway call failed. Never fatal — vetting degrades to Transport Pro."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class HighwayClient:
    """Carrier records from Highway. `transport` is injected by the tests."""

    def __init__(self, settings: Settings, *, transport: Any | None = None):
        base = settings.highway_api_url.rstrip("/")
        if not base:
            raise HighwayError("HIGHWAY_API_URL is not set.")
        self._settings = settings
        # The token is stored WITHOUT a "Bearer " prefix and prefixed here. Worth
        # tolerating both: the value is usually copied from somewhere that already
        # includes it, and a doubled "Bearer Bearer ey..." is a 401 that looks
        # exactly like an expired key.
        token = settings.highway_api_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        self._client = httpx.Client(
            base_url=base,
            timeout=httpx.Timeout(settings.highway_timeout),
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HighwayClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def carrier(self, *, mc: str | None = None, dot: str | None = None) -> dict | None:
        """The carrier's Highway record, or None if Highway doesn't have them.

        MC is tried as given; leading zeros are stripped because Highway keys on
        the bare number and `MC/0001594669` is a 404 where `MC/1594669` is a hit.

        A 404 means "not on Highway", which is a real answer and NOT an error —
        plenty of legitimate carriers on a broker's own books have no Highway
        record. Anything else raises `HighwayError` for the caller to swallow.
        """
        if mc:
            id_type, ident = "MC", str(mc).lstrip("0") or "0"
        elif dot:
            id_type, ident = "DOT", str(dot).lstrip("0") or "0"
        else:
            raise ValueError("Highway carrier lookup needs an mc or a dot number")

        path = f"/{id_type}/{ident}/by_identifier"
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise HighwayError(f"GET {path} failed: {exc}") from exc

        if response.status_code == 404:
            logger.info("Highway has no record for %s %s", id_type, ident)
            return None
        if response.status_code in (401, 403):
            # Worth its own message: this token is a JWT with a fixed expiry, so
            # "it worked last month" is the normal way this fails.
            raise HighwayError(
                f"Highway rejected the API token (HTTP {response.status_code}). "
                "HIGHWAY_API_TOKEN is a JWT with a hard expiry — check it hasn't "
                "lapsed.",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise HighwayError(
                f"GET {path} -> HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HighwayError(f"GET {path} returned non-JSON") from exc
        return payload if isinstance(payload, dict) else None
