"""
Render one agent line across candidate voices, to pick by ear AND by latency.

    uv run python tools/audition_voices.py                    # the shortlist
    uv run python tools/audition_voices.py --deepgram-voices  # list Deepgram's 36
    uv run python tools/audition_voices.py mai:en-US-Harper:MAI-Voice-2

Writes `voice-samples/<model>__<voice>.wav` and prints latency beside each.

BOTH columns matter. Synthesis is not streamed, so the whole utterance is built
before the caller hears anything: at 0.64x real time a twelve-second load pitch is
seven seconds of silence, on top of the LLM and the endpointing delay. A voice that
sounds lovely at 0.6x is worse on the phone than a plain one at 0.12x.

Only Deepgram will tell you its voice names — an invalid one returns a 400 listing
all 36. Every other provider answers an opaque "Provider returned 400", which is
why the candidates below are a hand-verified list rather than something discovered.
"""

from __future__ import annotations

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

OUT_DIR = pathlib.Path("voice-samples")


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
