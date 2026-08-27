"""
The LiveKit adapter: does audio reach the caller as it arrives, or all at once?

`OpenRouterTTS.stream_pcm` is a SYNC generator over a sync httpx stream — kept
that way on purpose, so the warmup, the unit tests and `tools/audition_voices.py`
all keep working with an injected transport and no event loop. The worker
therefore pumps it on a thread and hands blocks back through a queue.

That bridge is the part with teeth: a thread, a queue, and a cancellation path. If
it buffers, the streaming work upstream bought nothing; if it leaks the thread, an
interrupted call keeps pulling audio nobody will hear; if it swallows an
exception, a failed voice turns into silence instead of a handoff. None of that
shows up in a transcript, so it is pinned here.
"""

from __future__ import annotations

import asyncio

import pytest

from lanevoice.telephony.worker import FILLER_LINES, _pcm_frames, _TTSStream


class _FakeEmitter:
    """Stands in for LiveKit's AudioEmitter, recording the call order."""

    def __init__(self):
        self.initialized = None
        self.pushes: list[bytes] = []
        self.flushed = 0

    def initialize(self, **kwargs):
        self.initialized = kwargs

    def push(self, data: bytes):
        assert self.initialized, "audio pushed before the emitter was initialized"
        self.pushes.append(data)

    def flush(self):
        self.flushed += 1


class _FakeModel:
    """A stand-in for OpenRouterTTS with the same two-member surface."""

    sample_rate = 24000

    def __init__(self, blocks, *, fail=None, block_forever=False):
        self._blocks = blocks
        self._fail = fail
        self._block_forever = block_forever
        self.stopped_early = False
        self.delivered = 0

    def stream_pcm(self, text, stop=None):
        for block in self._blocks:
            if stop is not None and stop():
                self.stopped_early = True
                return
            self.delivered += 1
            yield block
        if self._fail is not None:
            raise self._fail
        while self._block_forever:          # a provider that never finishes
            if stop is not None and stop():
                self.stopped_early = True
                return


def _stream(model) -> _TTSStream:
    """A _TTSStream without touching LiveKit's TTS base class or the network.

    `ChunkedStream.__init__` wants a real TTS, and building one would open a
    connection in `prewarm`. Only `_run` is under test and it reads just
    `self._model` and `self.input_text`, so those are set directly.
    """
    stream = object.__new__(_TTSStream)
    stream._model = model
    stream._input_text = "Alright, I've got it at $2450 on this one."
    return stream


def _run(stream, emitter):
    return asyncio.run(stream._run(emitter))


@pytest.fixture(autouse=True)
def _input_text_property(monkeypatch):
    """`input_text` is a read-only property on the real base class."""
    monkeypatch.setattr(_TTSStream, "input_text",
                        property(lambda self: self._input_text), raising=False)


def test_every_block_is_pushed_as_it_arrives():
    """Three blocks in, three pushes out — not one concatenated push, which is
    exactly the behaviour this replaced."""
    emitter = _FakeEmitter()
    _run(_stream(_FakeModel([b"aa", b"bb", b"cc"])), emitter)
    assert emitter.pushes == [b"aa", b"bb", b"cc"]
    assert emitter.flushed == 1


def test_the_emitter_is_set_up_before_any_audio():
    """Pushing before `initialize` is a crash inside LiveKit, and it would only
    happen on a real call."""
    emitter = _FakeEmitter()
    _run(_stream(_FakeModel([b"aa"])), emitter)
    assert emitter.initialized == {
        "request_id": "tts", "sample_rate": 24000,
        "num_channels": 1, "mime_type": "audio/pcm",
    }


def test_a_failed_voice_raises_instead_of_going_quiet():
    """The exception has to cross the thread boundary. Swallowed, the carrier
    hears silence and the call never gets handed to a rep."""
    model = _FakeModel([b"aa"], fail=RuntimeError("HTTP 400 for model"))
    emitter = _FakeEmitter()
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _run(_stream(model), emitter)
    assert emitter.pushes == [b"aa"]     # what did arrive still went out
    assert emitter.flushed == 0          # and the turn was not completed


def test_an_interrupted_turn_stops_the_thread_pulling_audio():
    """A cancelled turn must set the stop flag so the generator returns and the
    HTTP response closes — otherwise the thread outlives the turn, still pulling
    audio for a caller who stopped listening."""
    model = _FakeModel([b"aa", b"bb"], block_forever=True)
    emitter = _FakeEmitter()

    async def cancel_mid_turn():
        task = asyncio.ensure_future(_stream(model)._run(emitter))
        while not emitter.pushes:                  # let some audio through
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_mid_turn())
    assert model.stopped_early, "the generator was never told to stop"


def test_an_empty_utterance_still_completes():
    """A voice that returns no audio must not hang the turn."""
    emitter = _FakeEmitter()
    _run(_stream(_FakeModel([])), emitter)
    assert emitter.pushes == []
    assert emitter.flushed == 1


# --------------------------------------------------------------------------- #
# Dead-air filler clips
# --------------------------------------------------------------------------- #
def test_filler_pcm_is_framed_losslessly():
    """The cached clip must reach the caller byte-for-byte, in 20ms frames.

    A frame that drops or duplicates samples is an audible click on every
    single filler, and an odd trailing byte would build a corrupt AudioFrame.
    """
    sample_rate = 24000
    pcm = bytes(range(256)) * 400 + b"\x01\x02"   # not a multiple of the frame size

    async def collect():
        return [f async for f in _pcm_frames(pcm, sample_rate)]

    frames = asyncio.run(collect())
    step = int(sample_rate * 0.02) * 2
    assert all(len(bytes(f.data)) <= step for f in frames)
    assert all(f.sample_rate == sample_rate and f.num_channels == 1 for f in frames)
    rebuilt = b"".join(bytes(f.data) for f in frames)
    assert rebuilt == pcm[: len(rebuilt)]
    assert len(pcm) - len(rebuilt) < 2            # at most the odd half-sample


def test_filler_lines_carry_no_facts():
    """Fillers play in ANY call state, so they must never contain a number, a
    dollar sign, or anything that could collide with the money guardrail."""
    for line in FILLER_LINES:
        assert not any(ch.isdigit() for ch in line), line
        assert "$" not in line, line
