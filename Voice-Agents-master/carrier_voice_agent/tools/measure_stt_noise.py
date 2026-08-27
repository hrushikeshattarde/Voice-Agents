"""
Which transcriber survives a noisy phone line — and does prompt biasing help?

    uv run python tools/measure_stt_noise.py            # everything
    uv run python tools/measure_stt_noise.py --runs 3   # median latency too

`measure_latency.py` races the transcription models on CLEAN audio, which is a
ceiling. Real calls arrive as 8 kHz telephony audio with a truck around them,
and that is where the two models separate — and where OpenRouter's
`/audio/transcriptions` endpoint hurts most, because it IGNORES the vocabulary
prompt, so Whisper cannot be told to expect load numbers and MC numbers.

OpenRouter's audio-capable CHAT models (Gemini Flash family) DO honour
instructions. So this tool measures both routes on the same audio:

  * the same test lines as measure_latency.py, plus a load number and the short
    "Sure" that a VAD/STT chain most easily loses;
  * three conditions — clean 24 kHz, telephony (8 kHz band), and telephony with
    engine-shaped noise at 10 dB and 5 dB SNR;
  * a HIT/MISS column on the one token each line exists to carry (the digits,
    or the word) — because a model that is fast and drops a digit is not fast,
    it just moves the cost somewhere a stopwatch doesn't show.

Same caveat as its sibling: synthesized speech, synthetic noise. Treat the
result as a ranking, and re-check the winner against a real recording.
"""

from __future__ import annotations

import argparse
import base64
import io
import statistics
import time
import wave

import httpx
import numpy as np

from lanevoice.env import load_env
from lanevoice.settings import get_settings

# label, spoken line, the token the line exists to carry (digits, or a word)
LINES = [
    ("a rate, said out loud",
     "Can you do twenty-four seventy-five on that one? That's my last.", "2475"),
    ("an MC number",
     "Yeah, my MC is six one one three four nine.", "611349"),
    ("a load number",
     "I'm calling about load two five one three four four six.", "2513446"),
    ("a short answer",
     "Sure, that works.", "sure"),
]

# The /audio/transcriptions route (prompt ignored) …
TRANSCRIPTION_MODELS = [
    "openai/whisper-large-v3",
    "openai/whisper-large-v3-turbo",
]
# … and the /chat/completions route, where the vocabulary prompt is real.
CHAT_AUDIO_MODELS = [
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
]

CHAT_PROMPT = (
    "Transcribe this phone-call audio verbatim. It is one turn from a truck "
    "freight brokerage call. Expect and transcribe carefully: load numbers "
    "(usually seven digits), MC numbers (six or seven digits), USDOT numbers, "
    "dollar rates spoken like 'twenty-four seventy-five' (write 2475), and US "
    "city names. Write digits as digits. Output ONLY the transcription text — "
    "no labels, no quotes, no commentary."
)


# --------------------------------------------------------------------------- #
# audio conditions
# --------------------------------------------------------------------------- #
def _resample(audio: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    """Linear-interpolation resample — crude, which is the point: a phone call
    is not a mastering chain."""
    n_out = int(round(len(audio) * rate_out / rate_in))
    x_out = np.linspace(0.0, len(audio) - 1, n_out)
    return np.interp(x_out, np.arange(len(audio)), audio).astype(np.float32)


def telephony_band(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """24 kHz studio audio squeezed through an 8 kHz phone band and back."""
    return _resample(_resample(audio, sample_rate, 8000), 8000, sample_rate)


def engine_noise(n: int, seed: int = 7) -> np.ndarray:
    """Engine-shaped noise: brown noise (integrated white) for the rumble, a
    little white on top for road hiss. Synthetic so the tool needs no fixture
    files and every run degrades the audio identically."""
    rng = np.random.default_rng(seed)
    brown = np.cumsum(rng.standard_normal(n))
    brown /= np.max(np.abs(brown)) + 1e-9
    hiss = rng.standard_normal(n) * 0.15
    noise = brown + hiss
    return (noise / (np.max(np.abs(noise)) + 1e-9)).astype(np.float32)


def with_noise(audio: np.ndarray, snr_db: float) -> np.ndarray:
    noise = engine_noise(len(audio))
    speech_rms = float(np.sqrt(np.mean(audio**2))) + 1e-9
    noise_rms = float(np.sqrt(np.mean(noise**2))) + 1e-9
    noise *= (speech_rms / noise_rms) / (10 ** (snr_db / 20))
    mixed = audio + noise
    peak = np.max(np.abs(mixed)) + 1e-9
    return (mixed / max(peak, 1.0)).astype(np.float32)


def wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes((audio * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# the two transcription routes
# --------------------------------------------------------------------------- #
def transcribe_endpoint(settings, model: str, wav: bytes) -> tuple[str, float]:
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
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:100]}")
    return (response.json().get("text") or "").strip(), elapsed


def transcribe_chat(settings, model: str, wav: bytes) -> tuple[str, float]:
    started = time.monotonic()
    response = httpx.post(
        f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": CHAT_PROMPT},
                    {"type": "input_audio",
                     "input_audio": {
                         "data": base64.b64encode(wav).decode("ascii"),
                         "format": "wav",
                     }},
                ],
            }],
            "max_tokens": 200,
        },
        timeout=90,
    )
    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:100]}")
    text = (response.json()["choices"][0]["message"]["content"] or "").strip()
    return text, elapsed


def hit(transcript: str, key: str) -> bool:
    """Did the one token this line exists to carry survive?

    Digits are compared digits-to-digits ('24-75', '24/75' and '2475' all
    count); a word key just has to appear.
    """
    if key.isdigit():
        return key in "".join(ch for ch in transcript if ch.isdigit())
    return key.lower() in transcript.lower()


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1,
                        help="samples per cell (>=3 for meaningful latency medians)")
    args = parser.parse_args()

    load_env(verbose=True)
    settings = get_settings()
    settings.require("openrouter_api_key")

    from lanevoice.voice import OpenRouterTTS

    routes = ([(m, transcribe_endpoint) for m in TRANSCRIPTION_MODELS] +
              [(m, transcribe_chat) for m in CHAT_AUDIO_MODELS])
    scores: dict[str, list[bool]] = {m: [] for m, _ in routes}
    latencies: dict[str, list[float]] = {m: [] for m, _ in routes}

    tts = OpenRouterTTS(settings)
    try:
        for label, line, key in LINES:
            clean = tts.synthesize(line)
            phone = telephony_band(clean, tts.sample_rate)
            conditions = [
                ("clean 24 kHz", clean),
                ("telephony + noise 10 dB", with_noise(phone, 10.0)),
                ("telephony + noise 5 dB", with_noise(phone, 5.0)),
            ]
            print(f"\n{'=' * 78}\n  {label} — said: {line}\n{'=' * 78}")
            for condition, audio in conditions:
                wav = wav_bytes(audio, tts.sample_rate)
                print(f"\n  [{condition}]")
                for model, call in routes:
                    try:
                        samples = [call(settings, model, wav)
                                   for _ in range(max(1, args.runs))]
                    except Exception as exc:  # noqa: BLE001 - keep racing
                        print(f"    {model:<32} FAILED: {str(exc)[:60]}")
                        continue
                    text = samples[-1][0]
                    median = statistics.median(t for _, t in samples)
                    ok = hit(text, key)
                    scores[model].append(ok)
                    latencies[model].append(median)
                    mark = "HIT " if ok else "MISS"
                    print(f"    {model:<32} {median:>5.2f}s  {mark}  {text[:70]}")
    finally:
        tts.close()

    print(f"\n{'=' * 78}\n  SCOREBOARD — key-token hits across all lines and "
          f"conditions\n{'=' * 78}")
    for model, _ in routes:
        if not scores[model]:
            print(f"  {model:<32} no successful calls")
            continue
        rate = sum(scores[model]) / len(scores[model])
        lat = statistics.median(latencies[model])
        print(f"  {model:<32} {sum(scores[model]):>2}/{len(scores[model])}"
              f"  ({rate:>4.0%})   median {lat:.2f}s")
    print("\nPut the winner in .env: STT_MODEL=<model> — a chat-route winner "
          "needs the adapter (ask before wiring).")


if __name__ == "__main__":
    main()
