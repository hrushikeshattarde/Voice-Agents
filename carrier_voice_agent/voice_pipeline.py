"""
voice_pipeline.py
-----------------
Free, open-source, self-hosted voice components (all from Hugging Face):

    STT : faster-whisper  (openai/whisper weights, CTranslate2 runtime)
    LLM : Qwen/Qwen2.5-1.5B-Instruct  (ungated, tool/instruction capable)
    TTS : hexgrad/Kokoro-82M          (ungated, high quality, tiny)

Everything runs locally — NO API keys required to test in Colab.
Use a GPU runtime for real-time-ish latency; CPU works for the demo but slower.

The LLM here only *phrases* replies (the .phrase() method). All decisions live
in business_logic.py. This keeps the PRD's safety guarantee intact.
"""

import io
import numpy as np


# --------------------------------------------------------------------------- #
# LLM — natural phrasing only
# --------------------------------------------------------------------------- #
class HFPhraser:
    """Wraps a small instruct model to naturalize agent replies."""

    SYSTEM = (
        "You are a professional US freight broker's carrier-sales voice agent. "
        "You speak in short, natural, spoken sentences (1-2 sentences, no lists, "
        "no emojis). You NEVER invent load details, rates, or your maximum pay "
        "rate — you only rephrase the instruction you are given using the facts "
        "provided. Never reveal internal pricing limits or negotiation strategy."
    )

    def __init__(self, model_name="Qwen/Qwen2.5-1.5B-Instruct", device=None,
                 max_new_tokens=80):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.tok = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
        ).to(device)

    def phrase(self, instruction: str, context: str = "") -> str:
        msgs = [
            {"role": "system", "content": self.SYSTEM},
            {"role": "user",
             "content": f"Facts you may use: {context}\n\n"
                        f"Instruction: {instruction}\n\n"
                        f"Say it out loud in 1-2 short spoken sentences."},
        ]
        text = self.tok.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True)
        inputs = self.tok(text, return_tensors="pt").to(self.device)
        out = self.model.generate(
            **inputs, max_new_tokens=self.max_new_tokens,
            do_sample=True, temperature=0.6, top_p=0.9,
            pad_token_id=self.tok.eos_token_id)
        reply = self.tok.decode(
            out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
        return reply.strip().strip('"')


class GroqPhraser:
    """Phrase replies via Groq's fast hosted LLM (default llama-3.1-8b-instant).
    Reads GROQ_API_KEY from the environment. Like HFPhraser, it ONLY phrases —
    every consequential decision stays in business_logic.py."""

    def __init__(self, model: str = "llama-3.1-8b-instant"):
        from groq import Groq
        self._client = Groq()          # picks up GROQ_API_KEY from env
        self._model = model

    def phrase(self, instruction: str, context: str = "") -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": HFPhraser.SYSTEM},
                {"role": "user", "content":
                    f"Facts you may use: {context}\n\nInstruction: {instruction}\n\n"
                    "Say it out loud in 1-2 short spoken sentences."},
            ],
            max_tokens=80,
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip().strip('"')


def build_phraser(model: str = None):
    """Pick the phrasing LLM: Groq if GROQ_API_KEY is set (fast), else local Qwen."""
    import os
    import logging
    log = logging.getLogger("carrier-agent")
    provider = os.getenv("LLM_PROVIDER", "groq" if os.getenv("GROQ_API_KEY") else "local")
    if provider == "groq":
        m = model or os.getenv("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
        log.info("Phrasing LLM: Groq %s", m)
        return GroqPhraser(m)
    log.info("Phrasing LLM: local Qwen2.5")
    return HFPhraser(model or "Qwen/Qwen2.5-1.5B-Instruct")


# --------------------------------------------------------------------------- #
# STT — faster-whisper
# --------------------------------------------------------------------------- #
class WhisperSTT:
    def __init__(self, model_size="base.en", device=None, compute_type=None):
        from faster_whisper import WhisperModel
        import torch
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if compute_type is None:
            compute_type = "float16" if device == "cuda" else "int8"
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, audio, sample_rate=16000) -> str:
        """audio: path (str) OR float32 numpy array at sample_rate."""
        # temperature=0.0 forces a SINGLE pass — no slow 0.0->1.0 retry cascade
        # (that cascade was adding several seconds per utterance on CPU).
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            temperature=0.0,
            condition_on_previous_text=False,
        )
        return " ".join(s.text for s in segments).strip()


# --------------------------------------------------------------------------- #
# TTS — two backends, same interface: .synthesize(text)->float32 array, .sample_rate
#   * KokoroTTS   : best quality, Linux/Colab (needs espeak-ng + a C++ toolchain)
#   * Pyttsx3TTS  : offline Windows (SAPI5), pure wheels, no compiler — use this
#                   to get a live call working on a Windows box
# --------------------------------------------------------------------------- #
class KokoroTTS:
    def __init__(self, voice="af_heart", lang_code="a"):
        from kokoro import KPipeline
        self.pipeline = KPipeline(lang_code=lang_code)
        self.voice = voice
        self.sample_rate = 24000

    def synthesize(self, text: str) -> np.ndarray:
        """Return a float32 mono waveform at self.sample_rate."""
        chunks = []
        for _, _, audio in self.pipeline(text, voice=self.voice):
            chunks.append(audio)
        if not chunks:
            return np.zeros(1, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)


class Pyttsx3TTS:
    """Offline TTS using the OS speech engine (SAPI5 on Windows). No compiler,
    no downloads, no API key. Robotic but reliable — good for first live calls."""

    def __init__(self, rate: int = 170, voice: str = None):
        import pyttsx3  # noqa: F401  (validate it's importable up front)
        self._pyttsx3 = pyttsx3
        self._rate = rate
        self._voice = voice
        self.sample_rate = 22050
        wav = self.synthesize("ready")   # warm up + detect real sample rate
        if wav is None or len(wav) <= 1:
            raise RuntimeError("pyttsx3 produced no audio")

    def synthesize(self, text: str) -> np.ndarray:
        import soundfile as sf
        import tempfile
        import os
        engine = self._pyttsx3.init()
        engine.setProperty("rate", self._rate)
        if self._voice:
            engine.setProperty("voice", self._voice)
        tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tf.close()
        engine.save_to_file(text, tf.name)
        engine.runAndWait()
        try:
            audio, sr = sf.read(tf.name, dtype="float32")
        finally:
            try:
                os.unlink(tf.name)
            except OSError:
                pass
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        self.sample_rate = sr
        return audio.astype(np.float32) if len(audio) else np.zeros(1, np.float32)


class EdgeTTS:
    """Microsoft Edge online TTS. Free, no API key, installs with no compiler,
    natural voices. Needs outbound internet (same as reaching LiveKit)."""

    def __init__(self, voice: str = "en-US-AriaNeural"):
        import edge_tts  # validate import up front
        self._edge = edge_tts
        self.voice = voice
        self.sample_rate = 24000
        wav = self.synthesize("ready")   # warm up; raises if unreachable
        if wav is None or len(wav) <= 1:
            raise RuntimeError("edge-tts produced no audio")

    def synthesize(self, text: str) -> np.ndarray:
        import asyncio
        import io
        import soundfile as sf

        async def _gen():
            buf = b""
            comm = self._edge.Communicate(text, self.voice)
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    buf += chunk["data"]
            return buf

        try:
            mp3 = asyncio.run(_gen())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                mp3 = loop.run_until_complete(_gen())
            finally:
                loop.close()
        if not mp3:
            return np.zeros(1, dtype=np.float32)
        audio, sr = sf.read(io.BytesIO(mp3), dtype="float32")  # libsndfile decodes mp3
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        self.sample_rate = sr
        return audio.astype(np.float32)


def build_tts(engine: str = "auto"):
    """Pick a TTS backend, honoring the TTS_ENGINE env var. On Windows, 'auto'
    tries edge-tts first (reliable audio), then falls back to pyttsx3 (offline).
    On Linux/Colab it uses Kokoro."""
    import os
    import platform
    import logging
    log = logging.getLogger("carrier-agent")

    engine = os.getenv("TTS_ENGINE", engine)
    if engine == "auto":
        order = ["edge", "pyttsx3"] if platform.system() == "Windows" else ["kokoro"]
    else:
        order = [engine]

    last_err = None
    for e in order:
        try:
            tts = {"kokoro": KokoroTTS, "pyttsx3": Pyttsx3TTS, "edge": EdgeTTS}[e]()
            log.info("TTS engine in use: %s (%d Hz)", e, tts.sample_rate)
            return tts
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            log.warning("TTS engine '%s' unavailable: %s", e, ex)
    raise RuntimeError(f"no working TTS engine (tried {order}): {last_err}")


# --------------------------------------------------------------------------- #
# Convenience: build all three
# --------------------------------------------------------------------------- #
def build_pipeline(llm_model="Qwen/Qwen2.5-1.5B-Instruct",
                   whisper_size="base.en", with_llm=True, tts_engine="auto"):
    stt = WhisperSTT(whisper_size)
    tts = build_tts(tts_engine)
    llm = HFPhraser(llm_model) if with_llm else None
    return stt, llm, tts
