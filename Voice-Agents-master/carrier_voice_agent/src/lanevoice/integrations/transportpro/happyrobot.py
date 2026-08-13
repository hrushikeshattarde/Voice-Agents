"""
Transport Pro's "HappyRobot" endpoint — the two actions `publicapi` doesn't have.

    POST /svc/happyrobot.php   {"action": "...", "data": {...}}

Same host as the Public API, different path, and a completely different auth
model: a static bearer token rather than the `POST /auth` login. So it gets its
own thin client rather than being bolted onto `TransportProClient`, whose entire
token lifecycle would be dead weight here.

Three actions are used, each because nothing else in the stack can answer:

    accept_offer    -> `book_now_url`, the link the carrier opens to sign
    invite_carrier  -> the Highway connect invite for a carrier who passed
                       vetting but has no agreement with us yet
    carrier_lookup  -> the carrier's classification LIST, which `publicapi` does
                       not have. Verified against the live tenant:
                       `/voiceai/carrier_status` returns only
                       {carrier_name, city, state, dot_number, mc_number, id,
                       status} — no classifications at all. So this is the only
                       fallback for a classification Highway has no verdict on,
                       and the only source of `recent_load_count`.

One is deliberately NOT wrapped:

    available_loads -> measured against the live tenant, its
                       `required_carrier_classifications` was null on every load
                       where `publicapi` had a value. It is the weaker source
                       here, so the board query stays on `/load/search`.

`accept_offer` and `invite_carrier` WRITE. The first accepts a real offer on a
real load; the second emails a real carrier. Neither is safe to call
speculatively or to retry blindly, which is why there is no retry loop in this
file. `carrier_lookup` is a read.
"""

from __future__ import annotations

from typing import Any

import httpx

from lanevoice.logging_config import get_logger
from lanevoice.settings import Settings

logger = get_logger(__name__)


class HappyRobotError(RuntimeError):
    """A HappyRobot action failed."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class HappyRobotClient:
    """`accept_offer` and `invite_carrier`. `transport` is injected by the tests."""

    def __init__(self, settings: Settings, *, transport: Any | None = None):
        url = settings.happyrobot_url.strip()
        if not url:
            raise HappyRobotError("HAPPYROBOT_URL is not set.")
        self._settings = settings
        self._url = url
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.happyrobot_timeout),
            transport=transport,
            headers={
                "Authorization": f"Bearer {settings.happyrobot_token.strip()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # The endpoint has been observed to want a non-default agent.
                # Carried over verbatim from the email agent that proved it works.
                "User-Agent": "shane-says-hello",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HappyRobotClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _action(self, action: str, data: dict) -> dict:
        """One action. No retry — every action here has a side effect."""
        body = {"action": action, "data": data}
        try:
            response = self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            raise HappyRobotError(f"{action} failed: {exc}") from exc

        if response.status_code >= 400:
            raise HappyRobotError(
                f"{action} -> HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HappyRobotError(
                f"{action} returned non-JSON: {response.text[:200]}") from exc
        if not isinstance(payload, dict):
            raise HappyRobotError(f"{action} returned {type(payload).__name__}")

        # This endpoint reports failure in the BODY of a 200 — `response_code` 300
        # is "nothing matched", other 3xx values are real errors. A caller that
        # only checked the HTTP status would read every one of them as success.
        code = payload.get("response_code")
        if code is not None and int(code) >= 300:
            message = payload.get("message") or str(payload)[:200]
            raise HappyRobotError(f"{action} -> code {code}: {message}")
        return payload

    def carrier_lookup(self, *, mc: str | None = None,
                       dot: str | None = None) -> dict | None:
        """`carrier_lookup` -> the carrier record, including `classifications`.

        A READ. Leading zeros are stripped because this endpoint keys on the bare
        number, same as Highway.

        Returns None when nothing matched — `response_code` 300 with a "no
        carriers matching" message, which `_action` raises on, is caught here and
        turned into None because "not on file" is a real answer, not a failure.
        """
        if mc:
            data = {"mc_number": str(mc).lstrip("0") or "0"}
        elif dot:
            data = {"dot_number": str(dot).lstrip("0") or "0"}
        else:
            raise ValueError("carrier_lookup needs an mc or a dot number")
        try:
            payload = self._action("carrier_lookup", data)
        except HappyRobotError as exc:
            if "code 300" in str(exc):
                return None
            raise
        rows = payload.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return rows if isinstance(rows, dict) else None

    def accept_offer(self, offer_id: str) -> str | None:
        """`accept_offer` -> the carrier-facing booking link, or None.

        WRITES: this accepts a real offer against a real load. The returned URL is
        the only thing a carrier can actually be given — until they open it and
        sign, the load is not theirs, and telling them otherwise is the one lie
        this flow must never tell.

        None means the offer was accepted but no link came back, which is
        different from a failure and is handled differently by the caller: the
        rate IS on the record, so a rep can finish it.
        """
        payload = self._action("accept_offer", {"offer_id": int(str(offer_id).strip())})
        url = payload.get("book_now_url")
        if not url:
            # Sometimes nested under the usual envelopes.
            for key in ("data", "result"):
                inner = payload.get(key)
                if isinstance(inner, dict) and inner.get("book_now_url"):
                    url = inner["book_now_url"]
                    break
                if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                    url = (inner[0].get("book_now_url")
                           or (inner[0].get("carrier_sales_data") or {}).get(
                               "book_now_url"))
                    if url:
                        break
        if not url:
            logger.error("Transport Pro accepted offer %s but returned no "
                         "book_now_url (keys: %s)", offer_id, sorted(payload))
            return None
        return str(url)

    def invite_carrier(self, *, mc_number: str, email: str) -> bool:
        """`invite_carrier` -> the Highway connect invite. True if it was sent.

        WRITES, and outwardly: this sends an email to the carrier. Only ever
        called for a carrier whose status is NOT_CONNECTED — they passed the
        vetting rules and the connection is the only thing missing. Inviting a
        carrier who FAILED vetting would be an invitation to nothing.

        The address must be one already on the carrier's file. Never invite an
        address a caller read out on the phone: an unverified address plus a
        broker-branded onboarding link is a phishing vector aimed at a real
        carrier, and the whole point of the email gate is not to trust it.
        """
        if not email.strip():
            raise ValueError("invite_carrier needs an email address")
        mc = str(mc_number).lstrip("0") or "0"
        self._action("invite_carrier", {"mc_number": mc, "email": email.strip()})
        logger.info("Highway invite sent for MC %s to %s", mc, email)
        return True
