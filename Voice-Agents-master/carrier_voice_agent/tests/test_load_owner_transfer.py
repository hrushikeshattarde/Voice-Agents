"""A handoff goes to the rep who owns the load, and it is actually performed.

Transport Pro names a load's people under `internalContacts`; the carrier sales
rep there is whose call this is. Their name and number come from the Transport
Pro user record (or from the desk's `reps.toml` when it has a direct number for
them), the agent says who it is putting the caller through to, and the worker
then dials — a SIP transfer up the trunk. Until this existed the handoff was a
sentence: the state machine picked a name out of a table of invented reps, said
it, and the audit log wrote "connected" with nobody ever dialled.
"""

from __future__ import annotations

import httpx

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.integrations.transportpro.mappers import map_load, map_rep
from lanevoice.telephony.worker import sip_participant_identity, transfer_request
from lanevoice.voice import StubComposer
from tests.transportpro_fake import FakeTransportPro, board, repository
from tests.transportpro_fake import settings as tp_settings
from tests.transportpro_payloads import record_for

# The live shape of `GET /user/4507`, trimmed: a rep whose desk lists only the
# office main line with an extension.
USER_4507 = {
    "id": 4507, "firstName": "Salomon", "lastName": "Castillo",
    "title": "Carrier Sales Representative", "team": "CS-Panama",
    "assignedTerminal": {"id": 1159, "title": "Carrier Sales Team"},
    "phoneNumbers": [{"type": "OFFICE", "value": "260-208-4500 ext 1310"}],
    "emailContacts": [{"type": "MAIN", "value": "salomon.castillo@example.com"}],
}
CONTACTS = [{"type": "ORDERTAKER", "id": 2237}, {"type": "CARRIERSALESREP", "id": 4507}]


def test_the_carrier_sales_rep_on_the_load_is_its_owner():
    load = map_load(record_for(2551913, internalContacts=CONTACTS), posted=True)
    assert load.assigned_rep_id == "4507"
    only_taker = record_for(2551913, internalContacts=[{"type": "ORDERTAKER", "id": 2237}])
    assert map_load(only_taker, posted=True).assigned_rep_id == "2237"
    assert map_load(record_for(2551913), posted=True).assigned_rep_id is None


def test_a_user_record_becomes_a_rep_with_a_diallable_number():
    rep = map_rep(USER_4507)
    assert (rep.rep_id, rep.name, rep.available) == ("4507", "Salomon Castillo", True)
    assert rep.phone == "+12602084500"            # the main line; an extension cannot ride a REFER

    with_cell = dict(USER_4507, phoneNumbers=[
        {"type": "FAX", "value": "260-555-0100"},
        {"type": "OFFICE", "value": "260-208-4500"},
        {"type": "CELL", "value": "(260) 555-0199"},
    ])
    assert map_rep(with_cell).phone == "+12605550199"          # cell beats office; fax never
    assert map_rep(dict(USER_4507, phoneNumbers=[{"type": "FAX", "value": "260-555"}])).phone == ""
    assert map_rep({"id": 9, "firstName": "", "lastName": ""}) is None
    assert map_rep(None) is None


def test_the_owner_is_read_from_transport_pro_and_the_directory_wins(repo):
    fake = FakeTransportPro()
    fake.json("/user/4507", USER_4507)
    tp = repository(fake, repo)

    rep = tp.get_rep("4507")
    assert rep.name == "Salomon Castillo" and rep.phone == "+12602084500"
    tp.get_rep("4507")
    assert len(fake.calls("/user/4507")) == 1                  # cached: org structure
    assert tp.get_rep("jsmith") is None                        # not a Transport Pro id

    conn = repo._db.connect()
    conn.execute("INSERT INTO reps VALUES ('4507', 'Salomon Castillo', '+12605551234', 1)")
    conn.commit()
    conn.close()
    assert tp.get_rep("4507").phone == "+12605551234"          # reps.toml's direct number wins


def test_an_unreadable_user_costs_the_named_handoff_not_the_call(repo):
    fake = FakeTransportPro()
    fake.on("/user/4507", httpx.Response(502, text="bad gateway"),
            httpx.Response(502, text="bad gateway"))
    assert repository(fake, repo).get_rep("4507") is None


def _tp_agent(fake, repo, **overrides) -> CarrierSalesAgent:
    # The trunk moves calls in these tests: the wording under test is the one a
    # caller hears when the transfer is actually going to happen.
    settings = tp_settings().model_copy(update={"sip_transfer_enabled": True, **overrides})
    return CarrierSalesAgent(repository(fake, repo), StubComposer(), settings)


def test_a_handoff_names_the_loads_rep_and_hands_the_worker_their_number(repo):
    fake = FakeTransportPro()
    board(fake, record_for(1303369, internalContacts=CONTACTS))
    fake.json("/user/4507", USER_4507)
    conn = repo._db.connect()
    conn.execute("DELETE FROM reps")                          # no directory at all
    conn.commit()
    conn.close()
    agent = _tp_agent(fake, repo)
    agent.greeting()
    agent.handle("load 1303369")

    agent._transfer_and_say(reason="ceiling_guard")

    assert agent.summary()["outcome"] == "transferred"
    assert agent.pending_transfer.rep_id == "4507"
    assert agent.pending_transfer.phone == "+12602084500"
    last = agent._composer.turns[-1]
    assert "transferring them to Salomon Castillo" in last["directive"]
    assert "Salomon Castillo" in last["facts"]
    conn = repo._db.connect()
    try:
        events = [tuple(r) for r in conn.execute(
            "SELECT rep_id, transfer_result FROM transfer_events").fetchall()]
    finally:
        conn.close()
    assert events == [("4507", "requested")]                   # nothing claimed connected yet

    agent.note_transfer_result(agent.pending_transfer, True)
    conn = repo._db.connect()
    try:
        results = [r[0] for r in conn.execute(
            "SELECT transfer_result FROM transfer_events ORDER BY id").fetchall()]
    finally:
        conn.close()
    assert results == ["requested", "connected"]


def test_a_rep_with_no_number_is_named_but_promised_as_a_callback(repo):
    fake = FakeTransportPro()
    board(fake, record_for(1303369, internalContacts=CONTACTS))
    fake.json("/user/4507", dict(USER_4507, phoneNumbers=[]))
    conn = repo._db.connect()
    conn.execute("DELETE FROM reps")
    conn.commit()
    conn.close()
    agent = _tp_agent(fake, repo)
    agent.greeting()
    agent.handle("load 1303369")

    agent._transfer_and_say(reason="ceiling_guard")

    assert agent.pending_transfer is None
    directive = agent._composer.turns[-1]["directive"]
    assert "Salomon Castillo will call them straight back" in directive
    assert "putting them through" not in directive


def test_a_failed_transfer_is_recorded_as_such(repo):
    agent = CarrierSalesAgent(repo, StubComposer())
    agent.greeting()
    agent.handle("about L1001")
    agent._transfer_and_say(reason="ceiling_guard")
    rep = agent.pending_transfer
    assert rep is not None                                     # a seeded rep with a number
    agent.note_transfer_result(rep, False, "SIP 486 Busy Here")
    conn = repo._db.connect()
    try:
        results = [r[0] for r in conn.execute(
            "SELECT transfer_result FROM transfer_events ORDER BY id").fetchall()]
        notes = " ".join(r[0] for r in conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()
    assert results == ["requested", "failed: SIP 486 Busy Here"]
    assert "Could NOT transfer" in notes and "486" in notes


class _FakeSip:
    def __init__(self):
        self.requests = []

    async def transfer_sip_participant(self, request):
        self.requests.append(request)


class _FakeCtx:
    class api:                                  # noqa: N801 - mirrors JobContext.api.sip
        sip = _FakeSip()

    class room:                                 # noqa: N801
        name = "call-_+19303334183_x"

        class _P:
            identity, kind = "sip_+19303334183", 3

        remote_participants = {"sip": _P()}


def _worker_agent(repo, monkeypatch, *, transfer_enabled: bool):
    import asyncio

    from lanevoice.telephony import worker

    monkeypatch.setattr(worker, "_settings", worker._settings.model_copy(
        update={"sip_transfer_enabled": transfer_enabled}))
    ctx = _FakeCtx()
    ctx.api.sip = _FakeSip()
    agent = worker.CarrierAgent(repo, StubComposer(), tts=object(), ctx=ctx)
    agent.brain.greeting()
    agent.brain.handle("about L1001")
    agent.brain._transfer_and_say(reason="ceiling_guard")
    assert agent.brain.pending_transfer is not None
    return agent, ctx, asyncio


def _transfer_log(repo) -> list[str]:
    conn = repo._db.connect()
    try:
        return [r[0] for r in conn.execute(
            "SELECT transfer_result FROM transfer_events ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_with_the_switch_off_the_handoff_is_announced_but_nobody_is_dialled(repo, monkeypatch):
    agent, ctx, asyncio = _worker_agent(repo, monkeypatch, transfer_enabled=False)
    asyncio.run(agent._transfer_if_pending())
    assert ctx.api.sip.requests == []
    assert agent.brain.pending_transfer is None
    assert _transfer_log(repo) == ["requested", "not performed: SIP transfer off"]


def test_with_the_switch_on_the_worker_refers_the_sip_caller_to_the_rep(repo, monkeypatch):
    agent, ctx, asyncio = _worker_agent(repo, monkeypatch, transfer_enabled=True)
    rep = agent.brain.pending_transfer
    asyncio.run(agent._transfer_if_pending())
    (request,) = ctx.api.sip.requests
    assert request.participant_identity == "sip_+19303334183"
    assert request.transfer_to == f"tel:{rep.phone}"
    assert _transfer_log(repo) == ["requested", "connected"]


def test_the_transfer_request_names_the_sip_caller_and_the_reps_number():
    class Participant:
        def __init__(self, identity, kind):
            self.identity, self.kind = identity, kind

    class Room:
        name = "call-_+19303334183_TeLdRPdDoZka"
        remote_participants = {
            "web": Participant("dashboard-user", 0),
            "sip": Participant("sip_+19303334183", 3),          # PARTICIPANT_KIND_SIP
        }

    identity = sip_participant_identity(Room())
    assert identity == "sip_+19303334183"
    request = transfer_request(Room.name, identity, "+12602084500")
    assert request.room_name == Room.name
    assert request.participant_identity == "sip_+19303334183"
    assert request.transfer_to == "tel:+12602084500"
    assert request.play_dialtone is True

    class Empty:
        remote_participants = {}

    assert sip_participant_identity(Empty()) is None
