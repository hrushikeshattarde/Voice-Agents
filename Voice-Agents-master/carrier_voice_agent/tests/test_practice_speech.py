"""The speech legs of voice practice, exercised at the real wire shapes.

`httpx.MockTransport` plays OpenRouter, so the multipart transcription request,
the PCM decode, the WAV wrap and the error mapping are all the real code — the
`transportpro_fake` philosophy. What would break in production without these:
a clip uploaded without its model/prompt fields transcribes wrong or not at
all; bare PCM handed to a browser is unplayable silence; and a silent clip
that "succeeds" makes the customer answer a turn nobody took.
"""

from __future__ import annotations

import io
import wave

import httpx
import pytest

from lanevoice.practice.speech import PracticeSpeech
from lanevoice.settings import get_settings


def _settings(**overrides):
    # STT_MODEL is pinned here rather than inherited: it is a tuning knob a desk
    # legitimately changes in `.env`, and what's on trial is that the CONFIGURED
    # model is what goes over the wire — whichever one that is.
    return get_settings().model_copy(
        update={"openrouter_api_key": "test-key",
                "stt_model": "openai/whisper-test-model", **overrides})


def _speech(handler, **overrides) -> PracticeSpeech:
    return PracticeSpeech(_settings(**overrides),
                          transport=httpx.MockTransport(handler))


def _pcm_response(seconds: float, rate: int = 24000) -> httpx.Response:
    # 16-bit mono silence — enough to measure with, nothing to hear.
    return httpx.Response(
        200, content=b"\x00\x00" * int(rate * seconds),
        headers={"Content-Type": f"audio/pcm;rate={rate};channels=1"})


# ------------------------------------------------------------ transcription #
def test_transcribe_sends_the_configured_model_and_prompt():
    seen = {}

    def handle(request):
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "  Hey Dale, quick question.  "})

    text = _speech(handle).transcribe(b"\x1a\x45" * 800, "audio/webm;codecs=opus")
    assert text == "Hey Dale, quick question."
    assert seen["path"].endswith("/audio/transcriptions")
    # The multipart body carries the same model and vocabulary bias the phone
    # line uses, and the clip under a filename Whisper can key its decode on.
    assert b"whisper-test-model" in seen["body"]
    assert b"reefer" in seen["body"]           # STT_PROMPT rode along
    assert b"clip.webm" in seen["body"]


def test_a_silent_clip_is_a_valueerror_not_an_empty_turn():
    def handle(request):
        return httpx.Response(200, json={"text": "   "})

    with pytest.raises(ValueError, match="make out any speech"):
        _speech(handle).transcribe(b"\x00" * 800, "audio/webm")


def test_stt_http_errors_name_the_model_and_status():
    def handle(request):
        return httpx.Response(402, json={"error": "insufficient credits"})

    with pytest.raises(RuntimeError, match=r"402.*whisper-test-model"):
        _speech(handle).transcribe(b"\x00" * 800, "audio/webm")


# ---------------------------------------------------------------- synthesis #
def test_synthesize_wraps_bare_pcm_into_a_playable_wav():
    def handle(request):
        if request.url.path.endswith("/audio/speech"):
            return _pcm_response(1.0)
        raise AssertionError(f"unexpected call to {request.url.path}")

    wav_bytes, mime, secs = _speech(handle).synthesize("Prairie Steel, this is Dale.")
    assert mime == "audio/wav"
    assert wav_bytes[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        assert wav.getframerate() == 24000     # rate came from the Content-Type
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 24000
    assert secs == pytest.approx(1.0, abs=0.01)


def test_synthesize_survives_a_model_that_answers_with_a_container():
    # Some models ignore response_format=pcm and send WAV/MP3 anyway. The reply
    # must come out playable at the CONTAINER's rate, not the configured one.
    def handle(request):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 4000)     # half a second at 8 kHz
        return httpx.Response(200, content=buf.getvalue(),
                              headers={"Content-Type": "audio/wav"})

    wav_bytes, mime, secs = _speech(handle).synthesize("Short line.")
    assert mime == "audio/wav"
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        assert wav.getframerate() == 8000
    assert secs == pytest.approx(0.5, abs=0.01)


# ------------------------------------------------------------ configuration #
def test_voice_needs_the_openrouter_key_and_says_so():
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        PracticeSpeech(get_settings().model_copy(update={"openrouter_api_key": ""}))
