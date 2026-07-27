"""Groq hosted text-to-speech."""

from __future__ import annotations

import io

import numpy as np

from lanevoice.settings import Settings, get_settings


class GroqTTS:
    """Interface: .synthesize(text) -> float32 mono np.ndarray, plus .sample_rate."""

    def __init__(self, settings: Settings | None = None):
        from groq import Groq

        self._settings = settings or get_settings()
        self._client = Groq(api_key=self._settings.groq_api_key or None)
        self._model = self._settings.tts_model
        self._voice = self._settings.tts_voice
        self.sample_rate = 24000
        wav = self.synthesize("Ready.")   # warm up + detect real sample rate
        if wav is None or len(wav) <= 1:
            raise RuntimeError("Groq TTS produced no audio")

    def synthesize(self, text: str) -> np.ndarray:
        import soundfile as sf

        resp = self._client.audio.speech.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format="wav",
        )
        data = resp.read() if hasattr(resp, "read") else getattr(resp, "content", b"")
        if not data:
            return np.zeros(1, dtype=np.float32)
        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        self.sample_rate = sr
        return audio.astype(np.float32)
