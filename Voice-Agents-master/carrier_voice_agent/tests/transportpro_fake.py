"""
A scriptable fake Transport Pro, shared by the client, repository and flow tests.

It speaks the real wire format — Basic auth on login, bearer tokens after, the
`results` envelope — so the tests exercise the actual client rather than a stub of
it, and records every request for assertions.
"""

from __future__ import annotations

import base64

import httpx

from lanevoice.integrations.transportpro import (
    TransportProClient,
    TransportProRepository,
)
from lanevoice.settings import get_settings

BASE = "https://api.example.test/publicapi"
USERNAME = "apiuser"
PASSWORD = "secret"


def settings(**overrides):
    """Settings pointed at the fake, with Transport Pro as the data source."""
    return get_settings().model_copy(update={
        "data_source": "transportpro",
        "transport_pro_url": BASE,
        "transport_pro_username": USERNAME,
        "transport_pro_password": PASSWORD,
        "transport_pro_timeout": 2.0,
        # Off by default in tests: caching across cases would let one test's
        # answer leak into the next, and the cache has its own test.
        "transport_pro_load_cache_seconds": 0,
        "transport_pro_carrier_cache_seconds": 0,
        **overrides,
    })


class FakeTransportPro:
    """Queue responses per API path; the last queued response repeats."""

    def __init__(self, *, access="tok-1", refresh="ref-1", reject_refresh=False):
        self.requests: list[httpx.Request] = []
        self.access = access
        self.refresh = refresh
        self.reject_refresh = reject_refresh
        self.logins = 0
        self.refreshes = 0
        self.routes: dict[str, list] = {}

    def on(self, path, *responses):
        self.routes[path] = list(responses)
        return self

    def json(self, path, payload, status=200):
        return self.on(path, httpx.Response(status, json=payload))

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/auth"):
            if b"refresh_token" in (request.content or b""):
                self.refreshes += 1
                if self.reject_refresh:
                    return httpx.Response(401, json={"error": "bad refresh"})
                self.access = f"{self.access}+refreshed"
                return httpx.Response(200, json={"access_token": self.access,
                                                 "refresh_token": self.refresh})
            self.logins += 1
            expected = base64.b64encode(
                f"{USERNAME}:{PASSWORD}".encode()).decode()
            if request.headers.get("Authorization") != f"Basic {expected}":
                return httpx.Response(401, json={"error": "bad credentials"})
            return httpx.Response(200, json={"access_token": self.access,
                                             "refresh_token": self.refresh})

        # Routes are registered as API paths ("/voiceai/carrier_status") while the
        # request carries the base URL's prefix too ("/publicapi/voiceai/...").
        queued = next((q for route, q in self.routes.items()
                       if path.endswith(route) and q), None)
        if not queued:
            return httpx.Response(404, json={"error": "no route"})
        response = queued[0] if len(queued) == 1 else queued.pop(0)
        return response(request) if callable(response) else response

    # -- assertion helpers -------------------------------------------------- #
    def calls(self, path):
        return [r for r in self.requests if path in str(r.url)]

    def bodies(self, path):
        return [httpx.QueryParams((r.content or b"").decode())
                for r in self.calls(path)]

    def bearers(self, path):
        return [r.headers.get("Authorization") for r in self.calls(path)]


def board(fake, *records):
    """Put loads on the fake board the way the repository looks them up.

    `GET /load/{id}` for each one — the load lookup — and `GET /load/search` with
    all of them, which is where the agent gets the alternatives it reads out when
    a caller's number doesn't check out.
    """
    for record in records:
        load_id = record.get("id") or record.get("load_id")
        fake.json(f"/load/{load_id}", record)
    fake.json("/load/search", {"results": list(records)})
    return fake


def client(fake, **overrides):
    return TransportProClient(settings(**overrides), transport=fake.transport())


def repository(fake, audit, **overrides):
    """A `TransportProRepository` over the fake, with a real SQLite audit trail."""
    config = settings(**overrides)
    return TransportProRepository(
        TransportProClient(config, transport=fake.transport()), audit, config)
