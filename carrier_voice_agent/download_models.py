"""
download_models.py
-------------------
Pre-download every model the worker needs, ONCE, so a live call never waits on
a network download. After this runs, the weights live in your local Hugging Face
cache (~/.cache/huggingface) and load straight from disk forever after.

Run once, before starting the worker:

    uv run python download_models.py

Env toggles (optional):
    WHISPER_SIZE=base.en    # tiny.en is faster on CPU; base.en is more accurate
    AGENT_USE_LLM=1         # also pre-download the ~3GB Qwen phrasing model
"""

import os


def main():
    size = os.getenv("WHISPER_SIZE", "base.en")

    print(f"[1/3] Whisper STT ({size}) ...", flush=True)
    from faster_whisper import WhisperModel
    WhisperModel(size, device="cpu", compute_type="int8")  # downloads + caches
    print("      done.")

    print("[2/3] Silero VAD ...", flush=True)
    try:
        from livekit.plugins import silero
        silero.VAD.load()  # tiny, caches the turn-detection model
        print("      done.")
    except Exception as e:
        print(f"      skipped ({e})")

    if os.getenv("AGENT_USE_LLM") == "1":
        print("[3/3] Qwen phrasing LLM (~3GB, one time) ...", flush=True)
        from transformers import AutoModelForCausalLM, AutoTokenizer
        name = "Qwen/Qwen2.5-1.5B-Instruct"
        AutoTokenizer.from_pretrained(name)
        AutoModelForCausalLM.from_pretrained(name)
        print("      done.")
    else:
        print("[3/3] LLM skipped (AGENT_USE_LLM != 1) — using template replies.")

    print("\nAll set. Model weights are cached locally; the worker will now "
          "load them from disk with no download.")


if __name__ == "__main__":
    main()
