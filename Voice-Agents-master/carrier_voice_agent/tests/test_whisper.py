"""
The warm transfer: what the rep is told, and what their keypress means.

Two halves are tested here, and neither needs a phone line.

`whisper_script` is asserted on **content**, unlike every other thing the agent
says — because it is the one line that is not composed by a model. It carries a
load number, an MC number and dollar figures to a colleague who will act on them,
so the exact words are the contract.

`WhisperGate` is the keypress rules, kept pure so they are testable at all. What
cannot be tested here is the LiveKit plumbing in `whisper_and_bridge` — dialling
out, publishing audio, moving a participant between rooms — which needs a live
server and a real phone.
"""

import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.telephony.whisper import (
    WhisperAction,
    WhisperGate,
    _frames,
    whisper_room_name,
)
from lanevoice.voice import StubComposer
from lanevoice.voice.tts import speechify

EMPTY = "empty in Dallas, Texas today"


def _agent(repo, **overrides):
    settings = get_settings().model_copy(update={"max_negotiation_rounds": 6,
                                                 **overrides})
    return CarrierSalesAgent(repo, StubComposer(), settings=settings)


def _to_rate(repo, load="about L1001", mc="MC 123456"):
    a = _agent(repo)
    a.greeting()
    a.handle(load)
    a.handle(mc)
    a.handle(EMPTY)
    return a


def _notes(repo):
    conn = repo._db.connect()
    try:
        return " ".join(r["note"] for r in
                        conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()


def _transfers(repo):
    conn = repo._db.connect()
    try:
        return [(r["rep_id"], r["transfer_result"]) for r in conn.execute(
            "SELECT rep_id, transfer_result FROM transfer_events ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# What the rep hears
# --------------------------------------------------------------------------- #
def test_the_briefing_names_the_load_the_carrier_and_the_keypress(repo):
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    script = a.whisper_script()

    assert "Circle Logistics voice assistant" in script
    # Spelled out, and said TWICE — a rep is writing it down off a phone speaker.
    assert script.count("L 1 0 0 1") == 2
    assert "I repeat" in script
    assert "Chicago, IL to Dallas, TX" in script
    assert "Blue Sky Logistics LLC" in script
    assert "M C 1 2 3 4 5 6" in script
    assert "Press 9 to take the call, or 1 to hear this again." in script


def test_the_mc_prefix_is_not_read_out_twice(repo):
    """The record spells it "MC123456". Spelling the letters as well as saying
    "M C" put "M C M C 1 2 3 4 5 6" in the rep's ear."""
    a = _to_rate(repo)
    a.handle("put me through to a rep")
    assert "M C M C" not in a.whisper_script()


def test_the_briefing_says_where_the_negotiation_got_to(repo):
    """PRD §3.6b wants the offers made in the whisper. A rep who knows the carrier
    is at $2400 against our $2150 can open with a number; one who doesn't has to
    make the carrier start again, which is what a warm transfer exists to avoid."""
    a = _to_rate(repo)
    for turn in ("I need 2400", "still 2400", "2400 or I'm gone", "2400"):
        a.handle(turn)
    assert a.summary()["outcome"] == "transferred"
    script = a.whisper_script()
    assert "firm at $2400" in script
    assert "above what I can approve" in script
    assert "My last offer was $" in script


def test_a_figure_is_never_said_twice_in_the_briefing(repo):
    """Hearing the same number twice in one breath is how a rep writes it down
    wrong, so the reason sentence and the money sentence never both say the ask."""
    a = _to_rate(repo)
    for turn in ("I need 2400", "still 2400", "2400 or I'm gone", "2400"):
        a.handle(turn)
    assert a.whisper_script().count("$2400") == 1


def test_the_fraud_tripwire_is_explained_to_the_rep(repo):
    a = _to_rate(repo)
    a.handle("I'll haul it for 900")
    script = a.whisper_script()
    assert "$900" in script
    assert "flagged it instead of booking it" in script


def test_a_call_with_no_load_or_carrier_still_briefs_honestly(repo):
    """Asked for a person before giving a load number. The briefing says so rather
    than leaving the rep to guess what the silence means."""
    a = _agent(repo)
    a.greeting()
    a.handle("can I talk to a rep")
    script = a.whisper_script()
    assert "never got as far as a load number" in script
    assert "could not identify their company" in script
    assert "Press 9" in script


def test_the_briefing_carries_the_truck_so_the_rep_can_sell(repo):
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    assert "empty in Dallas, Texas today" in a.whisper_script()


def test_the_agreed_rate_is_briefed_when_a_booking_stalled(repo):
    """Rate agreed, address not on the account. The rep has to know a number was
    already promised or they will quote a different one."""
    a = _to_rate(repo)
    a.handle("that works")
    a.handle("yep I can cover it")
    a.handle("send it to newdesk at blueskylogistics dot com")
    a.handle("no, newdesk at blueskylogistics dot com")
    assert a.summary()["outcome"] == "transferred"
    script = a.whisper_script()
    assert "not on their account" in script
    assert "$2000" in script


def test_the_briefing_survives_the_voice(repo):
    """It reaches the rep through TTS, which rewrites numbers. The load number has
    to stay digit-by-digit and the money has to become words."""
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    spoken = speechify(a.whisper_script())
    assert "L 1 0 0 1" in spoken            # not "L one thousand and one"
    assert "two thousand dollars" in spoken  # not "$2000"
    assert "$" not in spoken


# --------------------------------------------------------------------------- #
# What the keypress means
# --------------------------------------------------------------------------- #
def test_the_accept_digit_takes_the_call():
    assert WhisperGate().on_digit("9") is WhisperAction.ACCEPT


def test_the_repeat_digit_replays_the_briefing_up_to_the_cap():
    gate = WhisperGate(max_repeats=2)
    assert gate.on_digit("1") is WhisperAction.REPEAT
    assert gate.on_digit("1") is WhisperAction.REPEAT
    # Past the cap it stops repeating — a rep leaning on 1 would hold a carrier on
    # a silent line indefinitely.
    assert gate.on_digit("1") is WhisperAction.IGNORE
    assert gate.repeats_left == 0


@pytest.mark.parametrize("pressed", ["8", "0", "#", "", None, " "])
def test_a_fumbled_key_is_not_a_refusal(pressed):
    """A rep reaching for 9 and hitting 8 has not declined the call. The decision
    timeout is what handles somebody who genuinely isn't there."""
    assert WhisperGate().on_digit(pressed) is WhisperAction.IGNORE


def test_the_digits_are_configurable():
    gate = WhisperGate(accept="2", repeat="3")
    assert gate.on_digit("2") is WhisperAction.ACCEPT
    assert gate.on_digit("3") is WhisperAction.REPEAT
    assert gate.on_digit("9") is WhisperAction.IGNORE


def test_a_keypress_arrives_with_whitespace_around_it():
    assert WhisperGate().on_digit(" 9 ") is WhisperAction.ACCEPT


# --------------------------------------------------------------------------- #
# The rep can't pick up: come back on the line and say so
# --------------------------------------------------------------------------- #
def test_a_rep_who_cannot_pick_up_is_reported_to_the_carrier_as_busy(repo):
    """The carrier is still on the line, holding. They are told the truth — the rep
    is tied up and will ring them — rather than being passed to a stranger or left
    listening to nothing."""
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    assert a.pending_transfer.rep.rep_id == "R01"      # L1001's assigned rep

    spoken = a.transfer_declined("no keypress after the briefing")
    assert spoken
    directive = a._composer.turns[-1]["directive"]
    assert "Sarah Chen is tied up right now" in directive
    assert "will call them back on this load" in directive
    # And it must not ask them to keep waiting for a connection that isn't coming.
    assert "NOT ask them to keep holding" in directive
    assert "NOT offer somebody else" in directive


def test_nobody_else_is_rung_after_a_rep_does_not_pick_up(repo):
    """One rep, one attempt. Being handed round three strangers is not what a
    carrier who asked for "the rep on this load" asked for."""
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    a.transfer_declined("no answer")
    assert a.pending_transfer is None
    assert _transfers(repo) == [("R01", "initiated"), ("R01", "declined")]


def test_the_rep_who_missed_the_call_is_recorded_as_owing_one(repo):
    """The carrier was promised a call back. Whoever reads the load has to know who
    owes it, or the promise is one nobody keeps."""
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    a.transfer_declined("voicemail picked up")
    notes = _notes(repo)
    assert "did not pick up the transfer" in notes
    assert "Sarah Chen OWES THIS CARRIER A CALL" in notes
    assert "+15551110101" in notes


class _Says:
    """A composer that says one fixed line, whatever it was asked for."""

    def __init__(self, line):
        self.line = line
        self.turns: list = []

    def compose(self, **_kwargs):
        return self.line

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


def test_a_hold_line_never_names_a_number(repo):
    """It is spoken while a handoff is in flight and is not checked by the usual
    money guard, so it refuses anything with a digit in it rather than trusting."""
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")

    a._composer = _Says("Bear with me, still trying to reach them.")
    assert a.still_holding() == "Bear with me, still trying to reach them."

    a._composer = _Says("Give me 2 more seconds.")
    assert a.still_holding() == ""

    a._composer = _Says("   ")
    assert a.still_holding() == ""


def test_a_composer_failure_during_the_hold_stays_quiet(repo):
    """`_say` would answer a dead composer by starting ANOTHER handoff, which would
    ring a second rep for a call already being answered by the first."""
    class _Broken:
        turns: list = []

        def compose(self, **_kwargs):
            raise RuntimeError("model down")

        def read(self, dialogue, fields):
            return dict.fromkeys(fields)

    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    before = a.pending_transfer
    a._composer = _Broken()
    assert a.still_holding() == ""
    assert a.pending_transfer is before      # no second handoff was started


# --------------------------------------------------------------------------- #
# Plumbing details that are easy to get wrong silently
# --------------------------------------------------------------------------- #
def test_each_rep_is_briefed_in_their_own_room():
    """Per rep, so an escalation gets a clean booth rather than inheriting the
    previous rep's leg."""
    assert whisper_room_name("call-abc", "2423") == "call-abc-whisper-2423"
    assert whisper_room_name("call-abc", "R02") != whisper_room_name("call-abc", "R01")


def test_the_last_frame_of_audio_is_padded_not_dropped():
    """The tail of the briefing is "press 9 to take the call". A short final frame
    is rejected by the SIP path, and dropping it clips exactly the part the rep
    needs."""
    frames = _frames(b"\x01\x02" * 500, 24000)
    assert len({len(f) for f in frames}) == 1
    assert sum(len(f) for f in frames) >= 1000


# --------------------------------------------------------------------------- #
# The worker's side: once two humans are talking, the agent must go quiet
# --------------------------------------------------------------------------- #
class _Room:
    """Stands in for the LiveKit room. Nothing here reaches a real one."""

    name = "call-test"
    remote_participants: dict = {}


class _Message:
    def __init__(self, text):
        self.text_content = text


def _worker_agent(repo):
    """A `CarrierAgent` with no session attached — enough to test the guards."""
    import lanevoice.telephony.worker as worker

    return worker.CarrierAgent(repo, StubComposer(), _Room())


def test_the_agent_does_not_speak_over_a_bridged_call(repo):
    """The state machine is DONE after a handoff, and its reply for DONE is "this
    call has ended, goodbye". Said out loud once the carrier and the rep are
    talking, that lands in the middle of their conversation."""
    import asyncio

    from livekit.agents import StopResponse

    agent = _worker_agent(repo)
    turns_before = len(agent.brain.transcript)
    agent._bridged = True

    with pytest.raises(StopResponse):
        asyncio.run(agent.on_user_turn_completed(None, _Message("so what's the rate")))
    # The brain never saw it: no transcript line, no state change.
    assert len(agent.brain.transcript) == turns_before


def test_a_handoff_is_only_attempted_once(repo):
    import asyncio

    agent = _worker_agent(repo)
    agent.brain.greeting()
    for turn in ("about L1001", "MC 123456", EMPTY, "can I talk to the sales rep"):
        agent.brain.handle(turn)
    assert agent.brain.pending_transfer is not None

    agent._handed_over = True          # as if the first attempt already ran
    asyncio.run(agent._hand_over_if_pending())
    # Still pending and untouched: nothing rang a second set of phones.
    assert agent.brain.pending_transfer is not None


def test_transfers_switched_off_leave_the_caller_on_the_line(repo):
    """`SIP_TRANSFER_ENABLED=0` is the PRD's phase-1 behaviour: announce and log the
    handoff, don't move anybody. It must not look like a failed transfer."""
    import asyncio

    import lanevoice.telephony.worker as worker

    original = worker._settings
    worker._settings = original.model_copy(update={"sip_transfer_enabled": False})
    try:
        agent = worker.CarrierAgent(repo, StubComposer(), _Room())
        agent.brain.greeting()
        for turn in ("about L1001", "MC 123456", EMPTY, "can I talk to the rep"):
            agent.brain.handle(turn)
        asyncio.run(agent._hand_over_if_pending())
        assert agent.brain.pending_transfer is None
        assert agent._bridged is False
        # No `failed` row — nothing was attempted, so nothing failed.
        assert [r for _, r in _transfers(repo)] == ["initiated"]
    finally:
        worker._settings = original


# --------------------------------------------------------------------------- #
# Only a caller who ASKED for a person is put through
#
# Everything else the agent can't finish is a callback: nobody's phone rings, and
# the carrier is told a rep will ring them. An unrequested transfer takes a call
# away from the carrier who was still using it and lands it on a rep who didn't
# ask for it.
# --------------------------------------------------------------------------- #
def test_asking_for_a_person_is_the_one_thing_that_dials(repo):
    a = _to_rate(repo)
    a.handle("can I talk to the sales rep")
    assert a.pending_transfer is not None
    assert _transfers(repo) == [("R01", "initiated")]
    assert "Transferring the caller to Sarah Chen" in _notes(repo)


@pytest.mark.parametrize("turns,reason", [
    (["I'll haul it for 900"], "fraud_review"),
    (["I need 2400", "still 2400", "2400 or I'm gone", "2400"],
     "above_agent_authority"),
    (["that works", "yep I can cover it",
      "send it to newdesk at blueskylogistics dot com",
      "no, newdesk at blueskylogistics dot com"], "email_not_verified"),
    (["that works", "I can't make that pickup"], "pickup_issue"),
])
def test_the_agent_never_dials_a_rep_uninvited(repo, turns, reason):
    a = _to_rate(repo)
    for turn in turns:
        a.handle(turn)
    assert a.summary()["outcome"] == "transferred"
    assert a._transfer_reason == reason
    # Handed to a human, but as a callback — no line is moved.
    assert a.pending_transfer is None
    assert _transfers(repo) == [("R01", "callback")]
    assert "CALLBACK OWED by Sarah Chen" in _notes(repo)


def test_a_callback_never_tells_the_carrier_to_hold(repo):
    """"Hold a moment" and "someone will ring you" are promises about different
    things. The wrong one is a carrier holding a line nobody is coming down."""
    a = _to_rate(repo)
    a.handle("I'll haul it for 900")
    directive = a._composer.turns[-1]["directive"]
    assert "will call them straight back" in directive
    assert "NOT tell them to hold" in directive
    assert "NOT say you're putting them through" in directive


def test_an_escalation_callback_is_still_hopeful(repo):
    """Their number is inside what the desk can pay — it is only above what the
    agent spends alone. A callback here must not sound like a rejection."""
    a = _to_rate(repo)
    for turn in ("I need 2400", "still 2400", "2400 or I'm gone", "2400"):
        a.handle(turn)
    directive = a._composer.turns[-1]["directive"]
    assert "NOT a no" in directive
    assert "Sarah Chen will call them straight back" in directive
    assert "sit tight" in directive
    assert "NOT tell them to hold" in directive


def test_a_carrier_who_asks_for_a_person_mid_escalation_does_get_dialled(repo):
    """The rule is about who asked, not about what happened earlier in the call."""
    a = _to_rate(repo)
    for turn in ("I need 2400", "still 2400"):
        a.handle(turn)
    a.handle("just put me through to the rep then")
    assert a._transfer_reason == "carrier_request"
    assert a.pending_transfer is not None


def test_a_missing_outbound_trunk_degrades_instead_of_failing(repo):
    """The rep can't be dialled and briefed without an outbound trunk. That must not
    tell a carrier who asked for a person that nobody could be reached — a blind
    transfer still connects them, it just gives the rep no context."""
    import asyncio

    import lanevoice.telephony.worker as worker

    original = worker._settings
    worker._settings = original.model_copy(update={
        "whisper_enabled": True, "livekit_sip_outbound_trunk_id": ""})
    attempted = []
    try:
        agent = worker.CarrierAgent(repo, StubComposer(), _Room(), tts_model=object())
        agent._blind_handover = lambda resolution: _record(attempted, resolution)
        agent.brain.greeting()
        for turn in ("about L1001", "MC 123456", EMPTY, "can I talk to the sales rep"):
            agent.brain.handle(turn)
        asyncio.run(agent._hand_over_if_pending())
    finally:
        worker._settings = original
    assert [r.rep.rep_id for r in attempted] == ["R01"]


async def _record(seen, resolution):
    seen.append(resolution)
