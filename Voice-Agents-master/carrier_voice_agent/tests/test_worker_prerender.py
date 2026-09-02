"""Clips rendered at process start: what the caller hears before any model speaks.

The greeting and the fillers are synthesised in prewarm and played from memory.
Two properties are pinned here without a network: a clip that fails to render
costs that clip and nothing else, and the sample rate stored with a clip is the
one the voice actually answered with — the OpenRouter voice reads its rate off the
response, and a clip played at the wrong rate is a voice at the wrong speed.
"""

from __future__ import annotations

import asyncio

from lanevoice.telephony import worker


class _FakeModel:
    """Stands in for `OpenRouterTTS`: a sync PCM generator and a sample rate."""

    def __init__(self, fail_on: str | None = None, sample_rate: int = 22050):
        self.fail_on = fail_on
        self.sample_rate = sample_rate
        self.calls: list[str] = []

    def stream_pcm(self, text, stop=None):
        self.calls.append(text)
        if text == self.fail_on:
            raise RuntimeError("OpenRouter /audio/speech -> HTTP 400")
        yield b"\x01\x02" * 240
        yield b"\x03\x04" * 240


def _plugin(model: _FakeModel) -> worker.OpenRouterTTSPlugin:
    # The real constructor warms the voice up over the network; bypass it.
    plugin = worker.OpenRouterTTSPlugin.__new__(worker.OpenRouterTTSPlugin)
    plugin._model = model
    return plugin


def test_openrouter_clips_carry_the_rate_the_voice_answered_with():
    model = _FakeModel(sample_rate=22050)
    clips = worker.prerender_clips(worker._settings, ["one", "two"], _plugin(model))
    assert [text for text, _, _ in clips] == ["one", "two"]
    assert {rate for _, _, rate in clips} == {22050}
    assert all(len(pcm) == 4 * 240 for _, pcm, _ in clips)
    assert model.calls == ["one", "two"]


def test_a_clip_that_fails_costs_that_clip_only():
    model = _FakeModel(fail_on="two")
    clips = worker.prerender_clips(worker._settings, ["one", "two", "three"], _plugin(model))
    assert [text for text, _, _ in clips] == ["one", "three"]


def test_run_blocking_works_with_and_without_a_running_loop():
    async def seven():
        return 7

    assert worker._run_blocking(seven()) == 7          # prewarm: no loop yet

    async def nested():
        return worker._run_blocking(seven())           # inside a loop: helper thread

    assert asyncio.run(nested()) == 7


def test_run_blocking_reraises_the_coroutines_error():
    async def boom():
        raise ValueError("no voice")

    try:
        worker._run_blocking(boom())
    except ValueError as exc:
        assert "no voice" in str(exc)
    else:
        raise AssertionError("expected the coroutine's error to surface")
