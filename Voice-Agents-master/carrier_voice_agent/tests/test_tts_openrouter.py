"""
The voice hop: what goes to `/audio/speech`, and what comes back off the wire.

Everything here is about a failure the transcript cannot show you. A wrong sample
rate, a stereo stream averaged the wrong way, a WAV header read as samples — the
log says the agent said "I've got it at $1600" and the carrier heard a chipmunk
or a burst of static. So the decode is pinned by test rather than by listening.

Driven through an `httpx.MockTransport`, so the real request body, the real
headers and the real decode all run with no network and no key.
"""

import struct

import numpy as np
import pytest

from lanevoice.settings import get_settings
from lanevoice.voice.tts import OpenRouterTTS, speechify

# Something long enough that a truncation bug shows up as a length mismatch.
_SAMPLES = [0, 8192, -8192, 32767, -32768, 1000, -1000, 3]


def _settings(**overrides):
    base = {"openrouter_api_key": "sk-or-test", "tts_voice": "",
            "tts_model": "fish-audio/s2.1-pro-free:free"}
    return get_settings().model_copy(update=base | overrides)


def _pcm(samples=_SAMPLES) -> bytes:
    """Signed 16-bit little-endian, the shape `/audio/speech` returns."""
    return struct.pack(f"<{len(samples)}h", *samples)


class _Recorder:
    """Answers every POST with the given body/headers and keeps the requests."""

    def __init__(self, content, content_type, status=200):
        self.content = content
        self.content_type = content_type
        self.status = status
        self.requests = []

    def transport(self):
        import httpx
        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        import httpx
        self.requests.append(request)
        return httpx.Response(self.status, content=self.content,
                              headers={"content-type": self.content_type})

    @property
    def body(self):
        import json
        return json.loads(self.requests[0].content)


def _tts(rec, **overrides):
    return OpenRouterTTS(_settings(**overrides), transport=rec.transport())


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def test_it_posts_to_audio_speech_with_the_configured_model():
    rec = _Recorder(_pcm(), "audio/pcm;rate=24000;channels=1")
    _tts(rec)

    request = rec.requests[0]
    assert str(request.url).endswith("/audio/speech")
    assert request.headers["authorization"] == "Bearer sk-or-test"
    assert rec.body["model"] == "fish-audio/s2.1-pro-free:free"
    assert rec.body["response_format"] == "pcm"


def test_an_unset_voice_is_omitted_rather_than_sent_empty():
    """`voice` is a required field on this endpoint, but an EMPTY one is a 400.
    Fish Audio has a default voice, so sending no voice at all is what lets the
    stock config synthesise; sending "" would fail every call."""
    rec = _Recorder(_pcm(), "audio/pcm;rate=24000;channels=1")
    _tts(rec, tts_voice="")
    assert "voice" not in rec.body


def test_a_configured_voice_is_sent_and_whitespace_trimmed():
    rec = _Recorder(_pcm(), "audio/pcm;rate=24000;channels=1")
    _tts(rec, tts_voice="  802e3bc2b27e49c2995d23ef70e6ac89  ")
    assert rec.body["voice"] == "802e3bc2b27e49c2995d23ef70e6ac89"


def test_the_text_is_speechified_before_it_reaches_the_voice():
    """The dollar and digit rewrites have to happen on THIS side of the request —
    they are the whole reason `$1600` isn't read out as "one hundred sixty"."""
    import json

    rec = _Recorder(_pcm(), "audio/pcm;rate=24000;channels=1")
    tts = _tts(rec)
    tts.synthesize("I've got it at $1600 on L1002.")

    sent = json.loads(rec.requests[-1].content)["input"]
    assert sent == speechify("I've got it at $1600 on L1002.")
    # No bare figure and no glued identifier survives to the voice.
    assert "$" not in sent
    assert "1600" not in sent
    assert "L1002" not in sent
    assert "dollars" in sent
    assert "L 1 0 0 2" in sent


# --------------------------------------------------------------------------- #
# The PCM decode
# --------------------------------------------------------------------------- #
def test_int16_pcm_becomes_float32_mono_in_range():
    rec = _Recorder(_pcm(), "audio/pcm;rate=24000;channels=1")
    audio = _tts(rec).synthesize("Ready.")

    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert len(audio) == len(_SAMPLES)
    assert np.all(np.abs(audio) <= 1.0)
    # 32768 as the divisor, so full-scale negative lands exactly on -1.0 and
    # nothing clips on the way to int16 in the LiveKit adapter.
    np.testing.assert_allclose(
        audio, np.array(_SAMPLES, dtype=np.float32) / 32768.0, atol=1e-7)


def test_the_sample_rate_comes_off_the_content_type():
    """PCM has no header, so the rate is only in the Content-Type. Taking the
    configured 24000 on a 16000 Hz response is a voice 50% too fast."""
    rec = _Recorder(_pcm(), "audio/pcm;rate=16000;channels=1")
    tts = _tts(rec, tts_sample_rate=24000)
    assert tts.sample_rate == 16000


def test_a_content_type_with_no_rate_falls_back_to_the_setting():
    rec = _Recorder(_pcm(), "audio/pcm")
    tts = _tts(rec, tts_sample_rate=22050)
    assert tts.sample_rate == 22050


def test_stereo_pcm_is_averaged_into_one_channel():
    """LiveKit is initialised with num_channels=1. Interleaved stereo passed
    through untouched plays at double speed with the channels alternating."""
    left, right = [4000, 8000, 12000], [0, 0, 0]
    interleaved = [s for pair in zip(left, right, strict=True) for s in pair]
    rec = _Recorder(_pcm(interleaved), "audio/pcm;rate=24000;channels=2")
    audio = _tts(rec).synthesize("Ready.")

    assert len(audio) == 3
    np.testing.assert_allclose(
        audio, np.array(left, dtype=np.float32) / 2 / 32768.0, atol=1e-7)


def test_a_truncated_final_sample_is_dropped_rather_than_raising():
    """An odd byte count would make `np.frombuffer` raise and lose the whole
    turn. One dropped sample is 1/24000 of a second."""
    rec = _Recorder(_pcm() + b"\x01", "audio/pcm;rate=24000;channels=1")
    audio = _tts(rec).synthesize("Ready.")
    assert len(audio) == len(_SAMPLES)


# --------------------------------------------------------------------------- #
# When the model doesn't do what it was asked
# --------------------------------------------------------------------------- #
def test_a_wav_response_is_decoded_rather_than_read_as_samples():
    """A model that ignores `response_format: pcm` sends a container. Reading its
    44-byte header as PCM is an audible crack of static in front of the speech."""
    import io

    import soundfile as sf
    wav = io.BytesIO()
    original = np.array(_SAMPLES, dtype=np.float32) / 32768.0
    sf.write(wav, original, 16000, format="WAV", subtype="PCM_16")

    rec = _Recorder(wav.getvalue(), "audio/wav")
    tts = _tts(rec, tts_sample_rate=24000)

    assert tts.sample_rate == 16000
    audio = tts.synthesize("Ready.")
    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, original, atol=1e-4)


def test_an_http_error_names_the_model_and_the_voice():
    """A 400 here is almost always a wrong model slug or a voice this model does
    not have, and the warm-up in __init__ turns it into a startup failure rather
    than silence on a live call. The message has to say which."""
    rec = _Recorder(b'{"error":{"message":"invalid voice"}}',
                    "application/json", status=400)
    with pytest.raises(RuntimeError, match="fish-audio/s2.1-pro-free:free"):
        _tts(rec, tts_voice="alloy")


def test_an_empty_body_is_reported_as_no_audio():
    rec = _Recorder(b"", "audio/pcm;rate=24000;channels=1")
    with pytest.raises(RuntimeError, match="produced no audio"):
        _tts(rec)


# --------------------------------------------------------------------------- #
# Streaming the response body
#
# The caller used to hear nothing until the last byte of the utterance had
# landed. PCM has no header, so any prefix is playable and each block can go
# straight out — measured on the gateway, that is 0.64s of silence removed from a
# full load pitch. The risk this trades for is byte alignment: a 16-bit sample
# split across two network chunks and reassembled wrong does not glitch, it turns
# the rest of the turn into white noise, and the transcript still reads perfectly.
# --------------------------------------------------------------------------- #
class _ChunkedRecorder(_Recorder):
    """Delivers the body in fixed-size network chunks, so sample boundaries fall
    INSIDE chunks the way they do on a real connection. 3 bytes is deliberately
    coprime with the 2-byte sample so every boundary lands mid-sample."""

    def __init__(self, content, content_type, chunk=3, status=200):
        super().__init__(content, content_type, status)
        self.chunk = chunk

    def _handle(self, request):
        import httpx
        self.requests.append(request)
        body, size = self.content, self.chunk

        def blocks():
            for i in range(0, len(body), size):
                yield body[i:i + size]

        return httpx.Response(self.status, content=blocks(),
                              headers={"content-type": self.content_type})


def test_samples_survive_chunk_boundaries():
    """The whole point: reassembly must be byte-exact even when every network
    chunk ends mid-sample."""
    samples = list(range(-3000, 3000, 7))
    rec = _ChunkedRecorder(_pcm(samples), "audio/pcm;rate=24000;channels=1", chunk=3)
    audio = _tts(rec).synthesize("anything")
    assert len(audio) == len(samples)
    assert np.allclose(audio, np.array(samples, dtype=np.float32) / 32768.0)


def test_audio_arrives_in_more_than_one_block():
    """If it all came out in one block there would be nothing streamed and the
    caller would wait exactly as long as before."""
    rec = _ChunkedRecorder(_pcm(list(range(5000))),
                           "audio/pcm;rate=24000;channels=1", chunk=3)
    blocks = list(_tts(rec).stream_pcm("a long turn"))
    assert len(blocks) > 1
    assert all(len(b) % 2 == 0 for b in blocks)      # never a split sample
    assert sum(len(b) for b in blocks) == 5000 * 2   # and nothing dropped


def test_an_abandoned_turn_stops_pulling_audio():
    """A caller who interrupts must not leave a thread draining the rest of the
    utterance into a queue nobody reads."""
    rec = _ChunkedRecorder(_pcm(list(range(20000))),
                           "audio/pcm;rate=24000;channels=1", chunk=64)
    tts = _tts(rec)
    seen = []
    for block in tts.stream_pcm("a very long turn", stop=lambda: len(seen) >= 1):
        seen.append(block)
    assert len(seen) == 1                            # stopped after the first
    assert sum(len(b) for b in seen) < 20000 * 2     # well short of the whole


def test_a_stereo_stream_is_still_downmixed_across_chunks():
    """Reading a stereo stream as mono is the voice at half speed."""
    left_right = [1000, 3000, -2000, -4000, 500, 1500]
    rec = _ChunkedRecorder(_pcm(left_right), "audio/pcm;rate=24000;channels=2",
                           chunk=3)
    audio = _tts(rec).synthesize("anything")
    assert len(audio) == 3
    assert np.allclose(audio, np.array([2000, -3000, 1000],
                                       dtype=np.float32) / 32768.0, atol=1e-4)


def test_a_container_response_is_still_buffered_and_decoded():
    """A WAV header cannot be streamed — it has to be read before any sample
    means anything — so that path buffers, and must keep working."""
    pytest.importorskip("soundfile")
    import io as _io

    import soundfile as sf
    buffer = _io.BytesIO()
    sf.write(buffer, np.array(_SAMPLES, dtype=np.int16), 16000, format="WAV")
    rec = _ChunkedRecorder(buffer.getvalue(), "audio/wav", chunk=7)
    tts = _tts(rec)
    audio = tts.synthesize("anything")
    assert tts.sample_rate == 16000
    assert len(audio) == len(_SAMPLES)
