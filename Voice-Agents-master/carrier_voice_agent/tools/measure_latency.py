"""
Measure the two legs of a turn that aren't the voice: transcription and composition.

    uv run python tools/measure_latency.py           # both, all candidates
    uv run python tools/measure_latency.py --stt     # transcription only
    uv run python tools/measure_latency.py --llm     # composition only

`audition_voices.py` covers the third leg. Together they account for everything
the caller waits on except the endpointing delay, which is a setting rather than
a model (MIN/MAX_ENDPOINTING_DELAY).

WHY BOTH COLUMNS AGAIN. For TTS the trade was latency against how human the voice
sounds. Here it is latency against being RIGHT, and on this desk being right is
measured in spoken numbers:

  * A rate misheard is a rate the negotiator never sees. "Twenty-four
    seventy-five" came back from whisper-large-v3 as the fraction "24/75",
    the ask never reached the engine, and the call ended at a human.
  * An MC misheard is a carrier asked to repeat themselves until they hang up.

So the test lines below are a spoken rate and a spoken MC number — the two
things these calls are actually made of — and the transcript is printed next to
the latency. A model that is 300ms faster and drops a digit is not faster; it
just moves the cost somewhere that doesn't show up in a stopwatch.

The audio is synthesised with the configured TTS voice rather than recorded, so
this is repeatable and needs no fixtures. It is therefore a CLEAN-LINE test: real
phone audio is 8kHz, compressed, and noisy, so treat these transcripts as an
upper bound on accuracy and re-check the winner against a real recording.
"""

from __future__ import annotations

import io
import statistics
import sys
import time
import wave

import httpx

from lanevoice.env import load_env
from lanevoice.settings import get_settings

# What carriers actually say, and what the parser has to survive. Both of these
# have a known-correct reading, so the transcript column is checkable by eye.
LINES = [
    ("a rate, said out loud", "Can you do twenty-four seventy-five on that one? "
                              "That's my last."),
    ("an MC number", "Yeah, my MC is six one one three four nine."),
]

# Transcription models OpenRouter fronts. large-v3 is what ships; turbo has a
# 4-layer decoder instead of 32, so it SHOULD be faster — that claim is exactly
# what this tool exists to check, because it also costs ~25x more per minute.
STT_CANDIDATES = [
    "openai/whisper-large-v3",
    "openai/whisper-large-v3-turbo",
]

# Composer models. Haiku 4.5 is what ships and is the fastest Claude tier; Sonnet
# 5 is here because a rejected turn costs a whole extra round trip, so the fast
# model is only the quick one if it also obeys the money guardrail.
LLM_CANDIDATES = [
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-5",
]

# The longest thing the agent says, for the voice measurement — a full load
# rundown is where streaming has the most silence to remove.
PITCH = ("Alright, so that load's a full van of paper, forty thousand pounds, "
         "picking up Monday in Marion, Ohio and delivering Wednesday in Sioux "
         "Falls, South Dakota. It's 819 miles. I've got it at $2450 on this one. "
         "Does that work for you?")

RUNS = 3       # medians, because a single sample over a gateway is mostly noise.
# Composition needs more samples than timing does: the figure that decides the
# model is a PASS RATE against the money guardrail, and 3 runs cannot tell 60%
# from 100%. Still small enough to re-run whenever a prompt changes.
LLM_RUNS = 8

# A real negotiation turn, not a toy prompt: the composer's latency scales with
# what it is actually asked to do, and this is the shape of the commonest turn.
DIRECTIVE = (
    "They asked for $2600. Say you can't get there and restate that YOUR number "
    "is $2450. You ALREADY know their truck empties in Ohio — do NOT ask where "
    "they're coming out of. Refer to it and ask what's driving their number. Then "
    "ask how close they can get to $2450."
)
FACTS = (
    "Load 2513446, Marion to Sioux Falls, Van.\n"
    "Caller's company: General Transystems Inc\n"
    "Their truck: empty in Ohio at 12 p.m.\n"
    "You are holding at $2450."
)
DIALOGUE = (
    "You (Alex): Alright, so I've got you at $2450 on this load. Does that work?\n"
    "Caller: Can we make it 2600?"
)


def wav_bytes(audio, sample_rate: int) -> bytes:
    """float32 mono -> a 16-bit WAV the transcription endpoint will accept."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes((audio * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


def transcribe(settings, model: str, wav: bytes) -> tuple[str, float]:
    started = time.monotonic()
    response = httpx.post(
        f"{settings.openrouter_base_url.rstrip('/')}/audio/transcriptions",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        files={"file": ("turn.wav", wav, "audio/wav")},
        data={"model": model, "language": "en", "response_format": "json"},
        timeout=90,
    )
    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
    return (response.json().get("text") or "").strip(), elapsed


def measure_stt(settings) -> None:
    from lanevoice.voice import OpenRouterTTS

    print("=" * 78)
    print("TRANSCRIPTION — synthesised clean audio, so accuracy here is a CEILING")
    print("=" * 78)
    tts = OpenRouterTTS(settings)
    try:
        for label, line in LINES:
            audio = tts.synthesize(line)
            wav = wav_bytes(audio, tts.sample_rate)
            seconds = len(audio) / tts.sample_rate
            print(f"\n  {label} ({seconds:.1f}s of audio)")
            print(f"  said: {line}")
            for model in STT_CANDIDATES:
                try:
                    samples = [transcribe(settings, model, wav) for _ in range(RUNS)]
                except Exception as exc:  # noqa: BLE001 - report and keep going
                    print(f"    {model:<34} FAILED: {str(exc)[:38]}")
                    continue
                median = statistics.median(t for _, t in samples)
                print(f"    {model:<34} {median:>6.2f}s  heard: {samples[-1][0]}")
    finally:
        tts.close()


def measure_llm(settings) -> None:
    """Per-call latency AND guardrail compliance, because only their product is
    the number the caller waits on.

    A reply naming a figure the engine didn't sanction is rejected and
    re-prompted (`CarrierSalesAgent._say`), so a model that answers in 1.5s but
    complies 60% of the time is slower in practice than one that takes 3s and
    always complies — and after LLM_ATTEMPTS failures the call goes to a rep,
    which is not a latency cost at all but a lost load. This runs the REAL
    guardrail (`_breach`) over each reply rather than eyeballing it.
    """
    from lanevoice.conversation.agent import _breach, _speakable
    from lanevoice.voice.composer import build_composer

    # Exactly what the HOLD turn in `_apply_negotiation` authorises: our $2450
    # and their $2600, with $2450 required.
    amounts, must_say = {2450, 2600}, 2450
    source = f"{DIRECTIVE} {FACTS}"
    speakable = _speakable(amounts)
    attempts = max(1, settings.llm_attempts)

    print("\n" + "=" * 78)
    print(f"COMPOSITION — one negotiation turn x{LLM_RUNS}, checked against the real")
    print("money guardrail. 'effective' is what the caller actually waits on:")
    print(f"per-call latency x expected attempts, capped at LLM_ATTEMPTS={attempts}.")
    print("=" * 78)
    for model in LLM_CANDIDATES:
        config = settings.model_copy(update={"llm_model": model})
        composer = build_composer(config)
        timings, passed, examples = [], 0, []
        for _ in range(LLM_RUNS):
            started = time.monotonic()
            try:
                reply = composer.compose(directive=DIRECTIVE, facts=FACTS,
                                         dialogue=DIALOGUE, speakable=speakable)
            except Exception as exc:  # noqa: BLE001
                print(f"  {model:<34} FAILED: {str(exc)[:38]}")
                break
            timings.append(time.monotonic() - started)
            breach = _breach(reply, amounts, must_say, source, speakable)
            passed += breach is None
            examples.append((breach is None, ' '.join(reply.split())))
        if not timings:
            continue

        median = statistics.median(timings)
        rate = passed / len(timings)
        # Expected attempts before a compliant turn, capped like `_say`'s loop.
        expected = sum((1 - rate) ** n for n in range(attempts))
        handoff = (1 - rate) ** attempts

        print(f"\n  {model}")
        print(f"    per call   {median:>6.2f}s   (min {min(timings):.2f}s)")
        print(f"    complies   {passed}/{len(timings)}  ({rate:.0%})")
        print(f"    effective  {median * expected:>6.2f}s   "
              f"({expected:.2f} attempts avg)")
        print(f"    handoff    {handoff:>6.1%}   of turns exhaust all "
              f"{attempts} attempts")
        for ok, text in examples[:2]:
            print(f"      [{'PASS' if ok else 'FAIL'}] {text[:118]}")


def measure_tts(settings) -> None:
    """What the caller gains from streaming the voice, and what is left over.

    `audition_voices.py` measures a whole utterance, which is the right number for
    choosing a voice. This is the number that matters once one is chosen: how long
    the caller sits in silence before the FIRST audio arrives.

    The gap between the two columns is what streaming removed. What it cannot
    remove is the provider's generation time, which is close to a fixed cost here
    rather than proportional to length — so a short turn pays nearly as much as a
    long one, and splitting the text into sentences would pay it per sentence.
    """
    from lanevoice.voice import OpenRouterTTS

    print("\n" + "=" * 78)
    print("VOICE — first audio vs. complete. The gap is what streaming removed;")
    print("the 'first' column is the provider generating, which it cannot remove.")
    print("=" * 78)
    tts = OpenRouterTTS(settings)
    try:
        for label, line in (("short turn", LINES[0][1]), ("full load pitch", PITCH)):
            started = time.monotonic()
            first, total = None, 0
            for block in tts.stream_pcm(line):
                if first is None:
                    first = time.monotonic() - started
                total += len(block)
            complete = time.monotonic() - started
            seconds = total / 2 / max(1, tts.sample_rate)
            print(f"\n  {label} ({seconds:.1f}s of audio)")
            print(f"    first audio  {first:>6.2f}s   <- what the caller waits")
            print(f"    complete     {complete:>6.2f}s")
            print(f"    streaming saved {complete - first:>5.2f}s of silence")
    finally:
        tts.close()


def main() -> None:
    load_env()
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set — nothing to measure.")
    args = sys.argv[1:]
    if not args or "--stt" in args:
        measure_stt(settings)
    if not args or "--tts" in args:
        measure_tts(settings)
    if not args or "--llm" in args:
        measure_llm(settings)
    print("\nPut the winners in .env:  STT_MODEL=<model>   LLM_MODEL=<model>")


if __name__ == "__main__":
    main()
