"""What happens around one of the agent's spoken lines on a phone call.

Pinned here against a fake session, each from a live call:

* A caller who cuts the pitch off is answered NEXT — the requirements
  follow-on is withdrawn, not read over them, and the brain is told it was
  never heard so it comes out on the next turn.
* The follow-on is composed WHILE the pitch plays and queued right behind it,
  so there is no silence between "got a couple of requirements" and the
  requirements for the caller to fill with "Okay." (09-09: that "Okay." killed
  the follow-on before a byte played, then counted as agreeing to it).
* A one-word answer that arrives while the agent is still finishing its
  sentence is committed as a turn once the audio ends, instead of being held
  and glued to whatever the caller says twelve seconds later ("Yes. Yes.").
  Older one-word transcripts are backchannels and are dropped, so they cannot
  turn the caller's next word into a two-word barge-in.
* A filler plays at most every FILLER_MIN_GAP_SECONDS, not on every slow turn.
* The greeting waits for the SIP leg to report active — it used to play into a
  leg that was still ringing.

Plus the two session settings that must reach the framework: the interruption
mode pinned (dev and production picked different ones) and the echo warm-up
off (it silenced the recogniser for the whole greeting).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from lanevoice.settings import get_settings
from lanevoice.telephony import worker
from lanevoice.voice import StubComposer


class _Speech:
    """What `session.say` hands back: awaitable, `interrupted`, and the text
    the framework aligned to the audio that actually played."""

    def __init__(self, interrupted: bool = False, heard: str | None = None,
                 blocking: bool = False, text=None):
        self.interrupted = interrupted
        self.chat_items = [SimpleNamespace(text_content=heard)] if heard is not None else []
        self.interrupt_calls = 0
        # A blocking handle "plays" until it is interrupted — a long pitch.
        self._release = asyncio.Event() if blocking else None
        # A streamed line: the pieces the session was fed, as they arrived.
        self.streamed: list[str] = []
        self._drained = None
        if text is not None and not isinstance(text, str):
            self._drained = asyncio.get_running_loop().create_task(self._drain(text))

    async def _drain(self, pieces):
        async for piece in pieces:
            self.streamed.append(piece)

    def interrupt(self):
        self.interrupt_calls += 1
        self.interrupted = True
        if self._release is not None:
            self._release.set()
        return self

    def __await__(self):
        async def _done():
            if self._release is not None:
                await self._release.wait()
            if self._drained is not None:
                await self._drained
        return _done().__await__()


class _Session:
    def __init__(self):
        self.said: list[str] = []
        self.handles: list[_Speech] = []
        self.commits: list[dict] = []
        self.cleared = 0
        self.current_speech = None
        self.user_state = "listening"
        self.interrupt_next = False

    def say(self, text, **_kw):
        self.said.append(text if isinstance(text, str) else "<streamed>")
        speech = _Speech(interrupted=self.interrupt_next, text=text)
        self.interrupt_next = False
        self.handles.append(speech)
        return speech

    def commit_user_turn(self, **kw):
        self.commits.append(kw)
        fut = asyncio.get_running_loop().create_future()
        fut.set_result("")
        return fut

    def clear_user_turn(self):
        self.cleared += 1


@pytest.fixture
def agent(repo, monkeypatch):
    session = _Session()
    monkeypatch.setattr(worker.CarrierAgent, "session", property(lambda self: session))
    a = worker.CarrierAgent(repo, StubComposer(), tts=None)
    a.brain.greet_with("Circle Logistics, this is Alex.")
    a.fake_session = session
    return a


def _final(text: str):
    return SimpleNamespace(transcript=text, is_final=True)


def _settings(monkeypatch, **overrides):
    monkeypatch.setattr(worker, "_settings", worker._settings.model_copy(update=overrides))


def _notes(agent) -> list[str]:
    conn = agent.brain._repo._db.connect()
    try:
        return [r[0] for r in conn.execute("SELECT note FROM call_notes").fetchall()]
    finally:
        conn.close()


PITCH = "Here's the load, got a couple of requirements to run through."
REQS = "Driver's gonna need to send the BOL as soon as they're loaded. Can you handle that?"


# --------------------------------------------------------------------------- #
# The follow-on: composed under the pitch, queued behind it, withdrawn on a cut
# --------------------------------------------------------------------------- #
def test_the_follow_on_is_queued_while_the_pitch_plays(agent, monkeypatch):
    agent.brain.pending_followup = True
    monkeypatch.setattr(agent.brain, "continue_turn", lambda: REQS)
    asyncio.run(agent._after_reply(agent.fake_session.say(PITCH), PITCH))
    # Both were said; the requirements were handed to the session to play
    # straight behind the pitch, not after a compose gap.
    assert agent.fake_session.said == [PITCH, REQS]
    assert agent.fake_session.handles[1].interrupt_calls == 0


def test_a_caller_who_cuts_the_pitch_gets_the_follow_on_withdrawn(agent, monkeypatch):
    agent.brain.pending_followup = True
    monkeypatch.setattr(agent.brain, "continue_turn", lambda: REQS)
    agent.fake_session.interrupt_next = True
    asyncio.run(agent._after_reply(agent.fake_session.say(PITCH), PITCH))
    # The follow-on was composed while the pitch played, so the pitch handle was
    # already cut when it was ready: it is never handed to the session. Their
    # words are the next turn.
    assert agent.fake_session.said == [PITCH]
    assert any("none of it played" in n and REQS in n for n in _notes(agent))


def test_a_follow_on_already_queued_behind_a_cut_pitch_is_interrupted(agent, monkeypatch):
    agent.brain.pending_followup = True
    monkeypatch.setattr(agent.brain, "continue_turn", lambda: REQS)
    pitch = _Speech(blocking=True)            # a long pitch, still playing

    async def run():
        # The follow-on is composed and queued behind the pitch first; the
        # caller cuts the pitch only afterwards.
        task = asyncio.ensure_future(agent._after_reply(pitch, PITCH))
        for _ in range(200):
            if agent.fake_session.said:
                break
            await asyncio.sleep(0.02)
        pitch.interrupt()
        await task

    asyncio.run(run())
    assert agent.fake_session.said == [REQS]
    follow = agent.fake_session.handles[0]
    assert follow.interrupt_calls == 1        # withdrawn before it played
    assert any("none of it played" in n and REQS in n for n in _notes(agent))


def test_a_cut_line_is_noted_with_what_was_heard(agent):
    speech = _Speech(interrupted=True, heard="A long")
    cut = asyncio.run(agent._speech_finished(speech, "A long pitch about a load."))
    assert cut is True
    assert any('heard up to: "A long"' in note for note in _notes(agent))


# --------------------------------------------------------------------------- #
# The brain takes back what the caller never heard
# --------------------------------------------------------------------------- #
def _in_requirements(brain, read: bool):
    brain.state = type(brain.state).CHECK_REQUIREMENTS
    brain._load_revealed = True
    brain._pitch_line = PITCH
    brain.transcript.append(("agent", PITCH))
    brain._turn_meta.append({"t": 1.0, "latency": None})
    if read:
        brain._requirements_read = True
        brain._requirements_line = REQS
        brain.transcript.append(("agent", REQS))
        brain._turn_meta.append({"t": 2.0, "latency": None})


def test_requirements_cut_before_a_word_played_are_unread(agent):
    b = agent.brain
    _in_requirements(b, read=True)
    b.note_playback_cut(REQS, "")
    assert b._requirements_read is False          # they will be read again
    assert b.transcript[-1] == ("agent", PITCH)   # the unheard line is gone from the dialogue
    assert len(b.transcript) == len(b._turn_meta)
    assert any("will be read again" in n for n in _notes(agent))


def test_requirements_mostly_unheard_are_unread_and_the_record_shows_what_played(agent):
    b = agent.brain
    _in_requirements(b, read=True)
    b.note_playback_cut(REQS, "Driver's gonna need")
    assert b._requirements_read is False
    assert b.transcript[-1] == ("agent", "Driver's gonna need [cut off by the caller]")


def test_requirements_mostly_heard_stand(agent):
    b = agent.brain
    _in_requirements(b, read=True)
    heard = "Driver's gonna need to send the BOL as soon as they're loaded. Can you"
    b.note_playback_cut(REQS, heard)
    assert b._requirements_read is True           # they heard the substance; a "yes" counts
    assert b.transcript[-1] == ("agent", f"{heard} [cut off by the caller]")


def test_a_pitch_cut_before_it_was_heard_is_given_again(agent):
    b = agent.brain
    _in_requirements(b, read=False)
    b.note_playback_cut(PITCH, "")
    assert b._load_revealed is False              # `_check_requirements` re-pitches
    assert ("agent", PITCH) not in b.transcript


def test_an_unrelated_cut_line_only_corrects_the_record(agent):
    b = agent.brain
    _in_requirements(b, read=True)
    b.transcript.append(("agent", "Sure, one sec."))
    b._turn_meta.append({"t": 3.0, "latency": None})
    b.note_playback_cut("Sure, one sec.", "")
    assert b._requirements_read is True
    assert b._load_revealed is True
    assert b.transcript[-1] == ("agent", REQS)


# --------------------------------------------------------------------------- #
# The short transcript the framework held while we were talking
# --------------------------------------------------------------------------- #
def test_a_short_answer_heard_under_our_last_words_is_committed_when_we_stop(agent):
    agent.on_user_input_transcribed(_final("Yes."))
    asyncio.run(agent._speech_finished(agent.fake_session.say("Can you handle both?"),
                                       "Can you handle both?"))
    assert agent.fake_session.commits == [{"transcript_timeout": 0.3}]
    assert agent.fake_session.cleared == 0
    assert agent._pending_final is None                  # consumed


def test_nothing_pending_means_nothing_done(agent):
    asyncio.run(agent._settle_withheld_transcript(more_coming=False))
    assert agent.fake_session.commits == []
    assert agent.fake_session.cleared == 0


def test_a_backchannel_from_earlier_in_the_pitch_is_dropped_not_glued(agent):
    # 09-09: "9." heard fourteen seconds into the pitch rode along with the
    # caller's later "Okay.", made it two words, and cancelled the follow-on.
    agent._pending_final = ("9.", time.monotonic() - worker._WITHHELD_ANSWER_WINDOW - 5)
    asyncio.run(agent._settle_withheld_transcript(more_coming=False))
    assert agent.fake_session.commits == []
    assert agent.fake_session.cleared == 1
    assert agent._pending_final is None


def test_an_acknowledgement_under_the_first_half_of_our_turn_is_dropped(agent):
    # "Okay" to "there's a couple of requirements to run through" acknowledges
    # the first half; it is neither the answer to the requirements nor
    # something to glue onto that answer.
    agent.on_user_input_transcribed(_final("Okay."))
    asyncio.run(agent._speech_finished(agent.fake_session.say(PITCH), PITCH, more_coming=True))
    assert agent.fake_session.commits == []
    assert agent.fake_session.cleared == 1


def test_nothing_is_touched_while_the_caller_is_still_talking(agent):
    agent.on_user_input_transcribed(_final("Yes."))
    agent.fake_session.user_state = "speaking"
    asyncio.run(agent._settle_withheld_transcript(more_coming=False))
    assert agent.fake_session.commits == []
    assert agent.fake_session.cleared == 0
    assert agent._pending_final is not None              # the normal path will take it


def test_no_commit_while_another_line_of_ours_is_still_playing(agent):
    agent.on_user_input_transcribed(_final("Yes."))
    agent.fake_session.current_speech = object()
    asyncio.run(agent._settle_withheld_transcript(more_coming=False))
    assert agent.fake_session.commits == []
    assert agent.fake_session.cleared == 0


def test_the_framework_never_withholds_when_the_word_rule_is_off(agent, monkeypatch):
    _settings(monkeypatch, min_interruption_words=0)
    agent.on_user_input_transcribed(_final("Yes."))
    asyncio.run(agent._settle_withheld_transcript(more_coming=False))
    assert agent.fake_session.commits == []
    assert agent.fake_session.cleared == 0


def test_a_committed_turn_clears_what_was_pending(agent):
    agent.on_user_input_transcribed(_final("Yes."))
    # The framework committed a turn: the handler is entered and rejects the
    # phantom, but whatever the recogniser produced is inside that turn now.
    with pytest.raises(worker.StopResponse):
        asyncio.run(agent.on_user_turn_completed(None, SimpleNamespace(text_content="you")))
    assert agent._pending_final is None


# --------------------------------------------------------------------------- #
# A whole turn, streamed: caller text in, sentences out as the model writes them
# --------------------------------------------------------------------------- #
class _StreamingComposer:
    """Plays one scripted reply delta by delta; records whether it was asked to."""

    def __init__(self, deltas):
        self.deltas = deltas
        self.streamed_calls = 0
        self.whole_calls = 0
        self.last_truncated = False
        self.turns: list[dict] = []

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.whole_calls += 1
        self.turns.append({"directive": directive})
        return "".join(self.deltas)

    def compose_stream(self, directive, facts="", dialogue="", speakable="", correction="",
                       already_said=""):
        self.streamed_calls += 1
        self.turns.append({"directive": directive})
        yield from self.deltas

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


def test_a_turn_is_spoken_sentence_by_sentence_while_the_brain_is_still_writing(
        repo, monkeypatch):
    _settings(monkeypatch, idle_prompt_seconds=0, filler_delay=0)
    composer = _StreamingComposer(["Got it, L1001. ", "Before we go on, can I ", "get your MC?"])
    session = _Session()
    monkeypatch.setattr(worker.CarrierAgent, "session", property(lambda self: session))
    a = worker.CarrierAgent(repo, composer, tts=None)
    a.brain.greet_with("Circle Logistics, this is Alex.")

    with pytest.raises(worker.StopResponse):
        asyncio.run(a.on_user_turn_completed(
            None, SimpleNamespace(text_content="calling about load L1001")))

    assert composer.streamed_calls == 1 and composer.whole_calls == 0
    assert session.said == ["<streamed>"]                    # one utterance, fed live
    assert session.handles[0].streamed == ["Got it, L1001. ",
                                           "Before we go on, can I get your MC? "]
    assert a.brain.transcript[-1] == ("agent", "Got it, L1001. Before we go on, can I get your MC?")
    assert a.brain.speech_sink is None                       # handed back after the turn


def test_with_the_switch_off_the_reply_is_said_whole(repo, monkeypatch):
    _settings(monkeypatch, idle_prompt_seconds=0, filler_delay=0, stream_compose=False)
    composer = _StreamingComposer(["Got it, L1001. ", "Can I get your MC?"])
    session = _Session()
    monkeypatch.setattr(worker.CarrierAgent, "session", property(lambda self: session))
    a = worker.CarrierAgent(repo, composer, tts=None)
    a.brain.greet_with("Circle Logistics, this is Alex.")

    with pytest.raises(worker.StopResponse):
        asyncio.run(a.on_user_turn_completed(
            None, SimpleNamespace(text_content="calling about load L1001")))

    assert composer.streamed_calls == 0 and composer.whole_calls == 1
    assert session.said == ["Got it, L1001. Can I get your MC?"]


# --------------------------------------------------------------------------- #
# A go-ahead the VAD heard but the recogniser lost
# --------------------------------------------------------------------------- #
def test_an_unheard_answer_to_the_pitch_reads_the_requirements_instead_of_re_asking(
        agent, monkeypatch):
    _settings(monkeypatch, unheard_reask_delay=0.01, idle_prompt_seconds=0)
    monkeypatch.setattr(agent.brain, "proceed_without_answer", lambda: REQS)
    asyncio.run(agent._reask_if_unheard(agent._turn_seq))
    assert agent.fake_session.said == [REQS]
    assert agent._reasks == 0                                # not a re-ask


def test_elsewhere_an_unheard_answer_is_still_re_asked(agent, monkeypatch):
    _settings(monkeypatch, unheard_reask_delay=0.01, idle_prompt_seconds=0)
    monkeypatch.setattr(agent.brain, "proceed_without_answer", lambda: None)
    agent._reask = None                                      # no clip: the words go through say()
    asyncio.run(agent._reask_if_unheard(agent._turn_seq))
    assert agent.fake_session.said == [worker.REASK_LINE]
    assert agent._reasks == 1


# --------------------------------------------------------------------------- #
# Filler cadence
# --------------------------------------------------------------------------- #
def test_a_filler_waits_out_the_gap_since_the_last_one(agent, monkeypatch):
    _settings(monkeypatch, filler_min_gap_seconds=30.0)
    assert agent._filler_due() is True                    # none played yet
    agent._last_filler_at = time.monotonic()
    assert agent._filler_due() is False                   # one just played
    agent._last_filler_at = time.monotonic() - 31
    assert agent._filler_due() is True


def test_gap_zero_is_the_old_behaviour(agent, monkeypatch):
    _settings(monkeypatch, filler_min_gap_seconds=0.0)
    agent._last_filler_at = time.monotonic()
    assert agent._filler_due() is True


def test_a_slow_reply_gets_one_filler_and_the_next_slow_reply_none(agent, monkeypatch):
    _settings(monkeypatch, filler_delay=0.01, filler_min_gap_seconds=30.0)
    agent._fillers = [("Alright, one sec.", b"\x00\x00" * 160, 8000)]

    async def slow():
        await asyncio.sleep(0.05)
        return "reply"

    async def two_turns():
        await agent._acknowledge_if_slow(asyncio.create_task(slow()))
        await agent._acknowledge_if_slow(asyncio.create_task(slow()))

    asyncio.run(two_turns())
    assert agent.fake_session.said == ["Alright, one sec."]


# --------------------------------------------------------------------------- #
# The greeting waits for the SIP leg
# --------------------------------------------------------------------------- #
def _sip_participant(status: str | None):
    attributes = {"sip.callStatus": status} if status else {}
    return SimpleNamespace(identity="sip_+12602649808", kind="SIP", attributes=attributes,
                           track_publications={})


def _with_room(agent, *participants):
    agent._ctx = SimpleNamespace(room=SimpleNamespace(
        remote_participants={p.identity + str(i): p for i, p in enumerate(participants)}))


def test_the_greeting_waits_until_the_leg_is_active(agent, monkeypatch):
    _settings(monkeypatch, sip_media_wait_seconds=5.0)
    participant = _sip_participant("ringing")
    _with_room(agent, participant)

    async def run():
        asyncio.get_running_loop().call_later(
            0.25, participant.attributes.__setitem__, "sip.callStatus", "active")
        started = time.monotonic()
        await agent._wait_for_caller_media()
        return time.monotonic() - started

    waited = asyncio.run(run())
    assert 0.2 <= waited < 2.0


def test_a_leg_that_never_turns_active_is_greeted_after_the_cap(agent, monkeypatch):
    _settings(monkeypatch, sip_media_wait_seconds=0.3)
    _with_room(agent, _sip_participant("ringing"))
    started = time.monotonic()
    asyncio.run(agent._wait_for_caller_media())
    assert 0.25 <= time.monotonic() - started < 2.0


def test_a_leg_already_active_and_a_non_phone_session_do_not_wait(agent, monkeypatch):
    _settings(monkeypatch, sip_media_wait_seconds=5.0)
    for participant in (_sip_participant("active"), _sip_participant(None)):
        _with_room(agent, participant)
        started = time.monotonic()
        asyncio.run(agent._wait_for_caller_media())
        assert time.monotonic() - started < 0.2
    agent._ctx = None                                      # the dashboard, the tests
    asyncio.run(agent._wait_for_caller_media())


def test_the_wait_can_be_switched_off(agent, monkeypatch):
    _settings(monkeypatch, sip_media_wait_seconds=0.0)
    _with_room(agent, _sip_participant("ringing"))
    started = time.monotonic()
    asyncio.run(agent._wait_for_caller_media())
    assert time.monotonic() - started < 0.2


# --------------------------------------------------------------------------- #
# What the session is handed
# --------------------------------------------------------------------------- #
def test_the_echo_warm_up_is_off_unless_asked_for():
    settings = get_settings().model_copy(update={"stt_provider": "openrouter"})
    assert worker.session_kwargs({}, settings)["aec_warmup_duration"] is None
    on = settings.model_copy(update={"aec_warmup_seconds": 2.0})
    assert worker.session_kwargs({}, on)["aec_warmup_duration"] == 2.0


def test_the_interruption_mode_is_pinned_and_normalised():
    settings = get_settings().model_copy(update={"interruption_mode": " Adaptive "})
    assert worker.turn_handling(settings)["interruption"]["mode"] == "adaptive"
    assert worker.turn_handling(get_settings())["interruption"]["mode"] == "vad"
