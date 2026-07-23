# Running LaneVoice on Google Colab — Step-by-Step

This guide walks you from zero to a working voice agent in Colab. **No API keys
are needed** for anything in this guide — the text and voice demos run entirely
on free, local Hugging Face models.

The notebook is **self-contained**: it writes all the Python modules itself, so
the only file you need to upload is `Voice_AI_Carrier_Agent_Colab.ipynb`.

---

## Part A — Get the notebook into Colab

Pick **one** of these.

### Option 1 — Upload the notebook (simplest)
1. Go to <https://colab.research.google.com>.
2. In the popup: **Upload** tab → **Choose file** → pick
   `Voice_AI_Carrier_Agent_Colab.ipynb` from this folder.
3. It opens ready to run. Skip to **Part B**.

### Option 2 — From Google Drive
1. Upload `Voice_AI_Carrier_Agent_Colab.ipynb` to your Google Drive.
2. Right-click it in Drive → **Open with → Google Colaboratory**.
   (If Colab isn't listed: *Open with → Connect more apps → install Colaboratory*.)

### Option 3 — You only have the `.py` files, not the notebook
You don't need the notebook at all. Make a blank notebook
(<https://colab.research.google.com> → **New notebook**) and in the **first cell**
upload the modules, then run them:
```python
from google.colab import files
files.upload()   # select: database.py business_logic.py conversation.py voice_pipeline.py run_demo.py
```
Then run the demo commands from **Part D / E** below.

---

## Part B — Set the runtime to GPU (recommended)

1. Menu: **Runtime → Change runtime type**.
2. **Hardware accelerator → T4 GPU** → **Save**.

> CPU works too — the text demo is instant either way, and the voice demo just
> runs a few seconds slower per reply on CPU. GPU mainly speeds up the first
> model load and TTS.

---

## Part C — Run the cells in order

Run each cell with **Shift+Enter**, top to bottom. Here's what each section does
and what to expect.

| Section | Cell does | Needs models? | Time |
|---|---|---|---|
| **1. Install** | `apt-get espeak-ng` + `pip install` the voice stack | — | ~1–3 min (first run) |
| **2. Write files** | Creates `database.py`, `business_logic.py`, etc. via `%%writefile` | No | instant |
| **3. Text demo** | Runs 3 scripted calls (book / floor-hold / revoked) | No | instant |
| **4. Interactive text** | You type carrier lines, agent replies in text | No | interactive |
| **5. Load voice models** | Downloads Whisper + Qwen2.5 + Kokoro | Yes | ~2–4 min first time (~1–2 GB) |
| **5b. Speak greeting** | Agent greeting is spoken via Kokoro | Yes | few sec |
| **5c. Talk to it** | Type a line → hear the spoken reply | Yes | few sec/turn |
| **5d. Real STT (optional)** | Upload a WAV → Whisper transcribes → agent replies by voice | Yes | few sec |
| **6. Inspect DB** | Shows the audit tables (calls, offers, transfers) | No | instant |
| **7. Deployment** | Writes `livekit_agent.py` (for real phone calls, off-Colab) | — | instant |

**If you only want to see the logic:** run sections 1–4 (skip the model install by
jumping straight to section 2; section 3/4 need no models).

**If you want to hear it talk:** run sections 1, 2, then 5.

---

## Part D — The text demo (no models, no keys)

After section 2 has written the files, section 3 runs automatically. You should
see scripted calls like:

```
SCENARIO: Happy path
AGENT : Thanks for calling the load desk. ... Which load are you calling about?
CALLER: calling about L1001
AGENT : Got it — load L1001, Chicago, IL to Dallas, TX ... I'll need your MC or USDOT number.
CALLER: MC 123456
AGENT : You're verified, Blue Sky Logistics LLC. The posted rate ... is $2400. ...
CALLER: can you do 2200?
AGENT : Done — you're booked on load L1001 at $2200. ...
```

**Try it yourself** (section 4). When the cell shows `YOU (carrier):`, type lines like:

| Type this | To test |
|---|---|
| `L1001` then `MC 123456` then `2100` | a normal booking |
| `L1003` then `MC654321` then `800` then `850` then `900` | floor protection (it will **not** book below $1000 — it transfers) |
| `L1002` then `MC999888` | revoked authority → routed to human review |
| `L1001` then `MC 123456` then `talk to a rep` | warm transfer |

Sample data you can use:

- **Open loads:** `L1001`, `L1002`, `L1003` (`L1004` is already covered)
- **Valid carriers:** `MC123456`, `MC654321`
- **Revoked carrier:** `MC999888` (fails verification on purpose)

---

## Part E — The voice demo 🔊

1. Run **section 5** (model load). First time it downloads weights — be patient.
2. Run the **speak greeting** cell — a small audio player appears. Click ▶ to hear it.
3. In the **"talk to it"** cell, edit the `carrier_line` box (it's a Colab form
   field) and re-run the cell for each turn. Each reply is both printed and spoken.
4. *(Optional)* the **Real STT** cell lets you upload a short mono `.wav` of you
   saying a carrier line; Whisper transcribes it and the agent answers by voice.

> **Audio doesn't autoplay?** That's a browser policy, not a bug — just click the
> ▶ button on the player. `autoplay` is off by default here on purpose.

---

## Part F — What runs where (and what needs keys)

| Task | Where | API keys |
|---|---|---|
| Text demo + interactive chat | Colab | **None** |
| Voice demo (hear/transcribe) | Colab | **None** |
| Real inbound phone calls | A small **GPU host** (not Colab) | LiveKit + Twilio (see below) |

Real phone calls don't run in Colab because Colab has no stable public endpoint
and sessions time out. For that, deploy `livekit_agent.py` (written by section 7)
on a small GPU box. Keys then required:

- **LiveKit** (free tier): `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
- **Twilio**: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, a Voice number + Elastic SIP Trunk
- *(optional)* `HF_TOKEN` for gated models, `FMCSA_WEBKEY` for real carrier verification

---

## Part G — Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: faster_whisper` / `kokoro` | You skipped **section 1**. Run the install cell, then re-run. |
| Kokoro errors about `espeak` / phonemes | Re-run the install cell — it runs `apt-get install espeak-ng`. Then **Runtime → Restart session** and re-run sections 1→5. |
| `NameError: stt / llm / tts is not defined` | Run **section 5** (model load) before the voice cells. |
| First voice cell is very slow / looks stuck | It's downloading ~1–2 GB of model weights on first run. Wait; later runs are cached and fast. |
| `RuntimeError: CUDA out of memory` | Use the smaller LLM or skip it: in section 5 call `build_pipeline(with_llm=False)`. Or **Runtime → Restart session** to clear GPU memory. |
| No GPU available / quota hit | Everything still works on CPU, just slower. Or **Runtime → Change runtime type → CPU**. |
| Interactive `input()` cell won't stop | It loops until the call reaches `DONE`. To stop early, click the ■ (interrupt) button, or type a line that ends the call (e.g. an accept or `talk to a rep`). |
| `from google.colab import files` fails | That cell only works inside Colab, not local Jupyter. |
| Audio player shows but is silent | Click ▶; check your system volume and that the browser tab isn't muted. |
| Want a cleaner run | **Runtime → Restart session and run all** after section 1 has installed once. |

---

## TL;DR

1. Upload `Voice_AI_Carrier_Agent_Colab.ipynb` to Colab.
2. Runtime → Change runtime type → **T4 GPU**.
3. Run cells top to bottom. Sections 3–4 = text demo (instant, no keys).
   Section 5 = hear it talk (free models, no keys).
4. Real phone calls = deploy `livekit_agent.py` off-Colab with LiveKit + Twilio keys.
