"""
Render one agent line across candidate voices, to pick by ear AND by latency.

    uv run python tools/audition_voices.py --inference        # LiveKit Inference shortlist
    uv run python tools/audition_voices.py --inference cartesia/sonic-3:<voice-id>
    uv run python tools/audition_voices.py                    # the OpenRouter shortlist
    uv run python tools/audition_voices.py --deepgram-voices  # list Deepgram's 36 (OpenRouter)
    uv run python tools/audition_voices.py mai:en-US-Harper:MAI-Voice-2

Writes `voice-samples/<model>__<voice>.wav` and prints latency beside each.

BOTH columns matter. On the OpenRouter path synthesis is not streamed, so the whole
utterance is built before the caller hears anything: at 0.64x real time a
twelve-second load pitch is seven seconds of silence, on top of the LLM and the
endpointing delay. A voice that sounds lovely at 0.6x is worse on the phone than a
plain one at 0.12x. On the Inference path the number that matters is the FIRST
column — how long until the first audio — because the rest plays while it streams.

Only Deepgram will tell you its voice names — an invalid one returns a 400 listing
all 36. Every other provider answers an opaque "Provider returned 400", which is
why the candidates below are a hand-verified list rather than something discovered.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys
import time
import wave

import httpx

from lanevoice.env import load_env
from lanevoice.settings import get_settings

# A real agent turn, not prose: a voice that reads paragraphs nicely can still
# mangle "two thousand eight hundred dollars" or read digits as a year.
LINE = ("Alright, so that load's a full van, Breckenridge, Minnesota to Akron, "
        "Colorado, picking up today. I've got it at two thousand eight hundred "
        "dollars, and you're only about fifty miles from the pickup. Want it?")

# (model, voice). Every one verified to return audio; latencies in the module
# docstring of `settings.py`. Ordered fastest first.
SHORTLIST = [
    ("microsoft/mai-voice-2-flash", "en-US-Ethan:MAI-Voice-2"),
    ("microsoft/mai-voice-2-flash", "en-US-Harper:MAI-Voice-2"),
    ("microsoft/mai-voice-2", "en-US-Ethan:MAI-Voice-2"),
    ("hexgrad/kokoro-82m", "am_adam"),
    ("hexgrad/kokoro-82m", "am_michael"),
    ("deepgram/aura-2", "aura-2-arcas-en"),
    ("deepgram/flux-tts:free", "flux-cole-en"),
    ("deepgram/flux-tts:free", "flux-drew-en"),
]

# LiveKit Inference "provider/model" + voice, for TTS_PROVIDER=inference. Every one
# verified to return audio through this project's gateway (see settings.py). Voices
# are ids on Cartesia and ElevenLabs, names on Deepgram Aura-2.
INFERENCE_SHORTLIST = [
    ("cartesia/sonic-3", "a167e0f3-df7e-4d52-a9c3-f949145efdab"),
    ("cartesia/sonic-3", "820a3788-2b37-4d21-847a-b65d8a68c99a"),
    ("cartesia/sonic-3", "729651dc-c6c3-4ee5-97fa-350da1f88600"),
    ("elevenlabs/eleven_flash_v2_5", "pNInz6obpgDQGcFmaJgB"),
    ("deepgram/aura-2", "aura-2-orion-en"),
]

OUT_DIR = pathlib.Path("voice-samples")


def audition_inference(settings, candidates: list[tuple[str, str]]) -> None:
    """Render LINE on each Inference voice; print first-audio, complete, and length.

    Runs outside a LiveKit job, so it binds its own HTTP session the way the
    framework documents for scripts (`utils.http_context.open`).
    """
    from livekit.agents import inference, utils

    async def run() -> None:
        print(f"  {'model':<30} {'voice':<26} {'first':>7} {'done':>7} {'audio':>7}")
        print("  " + "-" * 82)
        async with utils.http_context.open():
            for model, voice in candidates:
                tts = inference.TTS(model, voice=voice, language="en",
                                    api_key=settings.livekit_api_key,
                                    api_secret=settings.livekit_api_secret)
                started = time.monotonic()
                first = None
                pcm = bytearray()
                rate = None
                try:
                    async with tts.synthesize(LINE) as stream:
                        async for audio in stream:
                            if first is None:
                                first = time.monotonic() - started
                            pcm += audio.frame.data.tobytes()
                            rate = audio.frame.sample_rate
                except Exception as exc:  # noqa: BLE001 - report and keep going
                    print(f"  {model:<30} {voice[:26]:<26} FAILED: {str(exc)[:34]}")
                    continue
                finally:
                    await tts.aclose()
                complete = time.monotonic() - started
                if not pcm or not rate:
                    print(f"  {model:<30} {voice[:26]:<26} FAILED: no audio")
                    continue
                stem = f"{model.replace('/', '_')}__{voice}"
                with wave.open(str(OUT_DIR / f"{stem}.wav"), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(rate)
                    handle.writeframes(bytes(pcm))
                seconds = len(pcm) / 2 / rate
                print(f"  {model:<30} {voice[:26]:<26} {first:>6.2f}s {complete:>6.2f}s "
                      f"{seconds:>6.1f}s")

    asyncio.run(run())
    print(f"\nSamples in {OUT_DIR}/. Put the winner in .env:")
    print("  TTS_INFERENCE_MODEL=<model>\n  TTS_INFERENCE_VOICE=<voice>")


def deepgram_voices(settings) -> list[str]:
    """Deepgram's own list, harvested from the 400 an invalid voice provokes."""
    response = httpx.post(
        f"{settings.openrouter_base_url.rstrip('/')}/audio/speech",
        json={"model": "deepgram/flux-tts:free", "input": "x", "voice": "__bogus__"},
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=30,
    )
    return sorted(set(re.findall(r"flux-[a-z]+-[a-z]{2}", response.text)))


def main() -> None:
    load_env()
    settings = get_settings()
    args = sys.argv[1:]

    if "--deepgram-voices" in args:
        names = deepgram_voices(settings)
        print(f"{len(names)} voices on deepgram/flux-tts:free:")
        for i in range(0, len(names), 6):
            print("  " + "  ".join(f"{n:<20}" for n in names[i:i + 6]))
        return

    # `model:voice` pairs, or the shortlist.
    picked = [a for a in args if not a.startswith("-")]

    if "--inference" in args:
        settings.require("livekit_api_key", "livekit_api_secret")
        candidates = INFERENCE_SHORTLIST
        if picked:
            candidates = []
            for arg in picked:
                model, _, voice = arg.partition(":")
                if not voice:
                    raise SystemExit(f"expected provider/model:voice, got {arg!r}")
                candidates.append((model, voice))
        OUT_DIR.mkdir(exist_ok=True)
        print(f"line: {LINE[:64]}...\n")
        audition_inference(settings, candidates)
        return

    if picked:
        candidates = []
        for arg in picked:
            model, _, voice = arg.partition(":")
            if not voice:
                raise SystemExit(f"expected model:voice, got {arg!r}")
            candidates.append(("microsoft/mai-voice-2-flash" if model == "mai"
                               else model, voice))
    else:
        candidates = SHORTLIST

    OUT_DIR.mkdir(exist_ok=True)
    print(f"line: {LINE[:64]}...\n")
    print(f"  {'model':<30} {'voice':<26} {'lat':>7} {'audio':>7} {'xRT':>6}")
    print("  " + "-" * 82)

    from lanevoice.voice import OpenRouterTTS

    for model, voice in candidates:
        config = settings.model_copy(update={"tts_model": model, "tts_voice": voice})
        try:
            tts = OpenRouterTTS(config)
            started = time.monotonic()
            audio = tts.synthesize(LINE)
            elapsed = time.monotonic() - started
        except Exception as exc:
            print(f"  {model:<30} {voice:<26} FAILED: {str(exc)[:34]}")
            continue

        stem = f"{model.replace('/', '_').replace(':', '-')}__{voice.replace(':', '-')}"
        path = OUT_DIR / f"{stem}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(int(tts.sample_rate))
            handle.writeframes((audio * 32767).astype("<i2").tobytes())
        tts.close()

        seconds = len(audio) / tts.sample_rate
        print(f"  {model:<30} {voice:<26} {elapsed:>6.2f}s {seconds:>6.1f}s "
              f"{elapsed / seconds:>5.2f}x")

    print(f"\nSamples in {OUT_DIR}/. Put the winner in .env:")
    print("  TTS_MODEL=<model>\n  TTS_VOICE=<voice>")


if __name__ == "__main__":
    main()
