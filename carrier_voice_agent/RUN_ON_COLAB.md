# Running LaneVoice on Google Colab — Step-by-Step

This guide gets the carrier-sales voice agent running in Google Colab from
scratch. It is split into two parts:

- **Part A — The Colab demo** (text + voice). **Needs NO API keys.**
- **Part B — Real phone calls** (LiveKit + Twilio). Needs keys, runs off Colab.

---

## TL;DR — do I need API keys?

| What you want to do | Runs on Colab? | API keys needed |
|---|---|---|
| Text demo (full negotiation logic) | ✅ Yes | **None** |
| Voice demo (hear the agent, real speech-to-text) | ✅ Yes | **None** |
| Take real inbound phone calls | ❌ No (GPU host) | LiveKit + Twilio (see Part B) |

Whisper (STT), Qwen2.5 (phrasing), and Kokoro (TTS) are all **free, open-source,
ungated** Hugging Face models. They download automatically. No token, no account,
no billing to run the notebook.

---

# Part A — Run the demo on Colab (no keys)

### Step 1 — Open the notebook
Two options:

**Option 1 (upload the notebook):**
1. Go to <https://colab.research.google.com>
2. `File → Upload notebook` → choose `Voice_AI_Carrier_Agent_Colab.ipynb`.
3. That's it — the notebook writes every other file itself (nothing else to upload).

**Option 2 (upload the whole folder to Google Drive):**
1. Upload the `carrier_voice_agent` folder to your Google Drive.
2. Double-click `Voice_AI_Carrier_Agent_Colab.ipynb` → "Open with Google Colaboratory".

> The notebook is **self-contained**: its cells recreate `database.py`,
> `business_logic.py`, `conversation.py`, `voice_pipeline.py`, etc. via
> `%%writefile`. You do **not** need to upload the `.py` files separately.

### Step 2 — Turn on the GPU (recommended)
`Runtime → Change runtime type → Hardware accelerator → T4 GPU → Save`.
(CPU also works for the text demo and even the voice demo — just slower.)

### Step 3 — Run the cells top to bottom
Press `Shift+Enter` on each cell, in order:

| Section | What happens | Time |
|---|---|---|
| **1. Install** | Installs faster-whisper, transformers, torch, kokoro (+ espeak-ng). Skip if you only want the text demo. | 2–4 min |
| **2. Write files** | Creates the project modules. | instant |
| **3. Text demo** | Runs 3 scripted calls — booking, floor enforcement, revoked-authority. | instant |
| **4. Interactive chat** | You type carrier lines, agent replies in text. | instant |
| **5. Voice demo** | Downloads models (~1–2 GB first run), then the agent **speaks** its replies. | 1–3 min first run |
| **6. Audit trail** | Shows the `calls` / `offers` / `transfers` tables. | instant |
| **7. Deployment** | Writes `livekit_agent.py` + the Twilio/LiveKit key list (for later). | instant |

### Step 4 — Try it
- **Text chat cell:** type `L1001` → `MC 123456` → `2100` and watch it book.
- **Voice cell:** set the `carrier_line` field, run, and press play on the audio widget.
- **Real STT (optional):** record a short WAV saying "I'm calling about load L1001",
  upload it in the STT cell, and Whisper transcribes it.

### Sample data you can use in the demo
The carrier is *paid*, so the agent **opens low and walks its offer up** (~$25–30
per round). It will only go up to **ceiling − $150** (the $150 buffer is held for a
human). If the carrier asks **above the ceiling**, it logs a note and ends the call.

| Load | Lane | Agent opens at | Agent's max offer (=ceiling−150, hidden) | Hard ceiling (hidden) | Fraud-low (hidden) |
|---|---|---|---|---|---|
| `L1001` | Chicago → Dallas | $2000 | $2350 | $2500 | < $1400 |
| `L1002` | Atlanta → Miami | $1400 | $1700 | $1850 | < $1000 |
| `L1003` | LA → Phoenix | $900 | $1100 | $1250 | < $650 |

Try on L1001: say `yes` to book at $2000; or ask `2080` → it holds at $2000,
ask `2060` again → it comes up to $2025, say `deal`. Ask a high number like
`1500` on L1003 repeatedly → it holds firm, walks up a couple of times, then
**disconnects and writes a clear note** (it never hangs up on the first ask).
Ask `900` on L1001 → fraud review.

| Carrier number | Result |
|---|---|
| `MC 123456` / `DOT 1000001` | ✅ verified |
| `MC 654321` / `DOT 2000002` | ✅ verified |
| `MC 999888` | ❌ revoked → human review |
| `MC 777111` | ⚠️ reactivated (fraud flag) → human review |

### Colab gotchas
- **First voice run is slow** — it's downloading model weights. Later turns are fast.
- **Session resets** wipe everything (including `carrier_agent.db`). Just re-run the cells.
- **`input()` prompts** (chat cells) appear under the cell — click there to type.
- **No sound?** Colab audio widgets don't autoplay; press the ▶ on the widget.
- **"CUDA out of memory":** use `Runtime → Disconnect and delete runtime`, reconnect,
  or set `build_pipeline(with_llm=False)` to skip the LLM (template phrasing still works).

---

# Part B — Real phone calls (LiveKit + Twilio)

This does **not** run on Colab — Colab has no stable public endpoint and its
sessions time out. Deploy `livekit_agent.py` on a small **GPU host** (any cloud
VM with a T4/L4, or your own box). The worker dials **out** to LiveKit, so you
don't need to open any inbound ports.

### The API keys / accounts you need

#### 1. LiveKit (required) — open source; Cloud has a free tier
Sign up at <https://cloud.livekit.io> → create a project → copy from the project settings:

```
LIVEKIT_URL="wss://<your-project>.livekit.cloud"
LIVEKIT_API_KEY="APIxxxxxxxx"
LIVEKIT_API_SECRET="xxxxxxxxxxxxxxxx"
```
*Purpose:* real-time media transport + the SIP bridge your agent joins.
*Cost:* free tier to start, then usage-based. (Or self-host LiveKit — it's open source.)

#### 2. Twilio (required) — the actual phone number
Sign up at <https://www.twilio.com/try-twilio>, then:
- Buy a **Voice-capable phone number**.
- Create an **Elastic SIP Trunk** and point it at your LiveKit SIP URI.

```
TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxx"
TWILIO_AUTH_TOKEN="xxxxxxxxxxxxxxxx"
TWILIO_PHONE_NUMBER="+1XXXXXXXXXX"
```
*Purpose:* PSTN — the number carriers actually dial.
*Cost:* ~$1–2/mo per number + per-minute usage.

#### 3. Hugging Face token (OPTIONAL)
```
HF_TOKEN="hf_xxxxxxxx"
```
*Only needed if you swap in a **gated** model* (e.g. Meta Llama). The default
stack (Qwen2.5, Whisper, Kokoro) is ungated and needs **no** token.
Get one at <https://huggingface.co/settings/tokens>.

#### 4. FMCSA QCMobile WebKey (OPTIONAL, recommended for production)
```
FMCSA_WEBKEY="xxxxxxxx"
```
*Purpose:* replace the **mock** `verify_carrier()` with real carrier authority +
insurance data. Free, but requires a Login.gov developer account at
<https://mobile.fmcsa.dot.gov/developer/home.page>.
> Per PRD §8.1, FMCSA has had extended outages — add a commercial fallback
> (Highway / RMIS / MyCarrierPortal / DAT CarrierWatch) before you rely on it.

### Deploy steps
On the GPU host:
```bash
pip install "livekit-agents>=1.0" livekit-plugins-silero \
            faster-whisper kokoro soundfile transformers torch

export LIVEKIT_URL="wss://<project>.livekit.cloud"
export LIVEKIT_API_KEY="..."
export LIVEKIT_API_SECRET="..."

python livekit_agent.py start
```
Then wire Twilio → LiveKit SIP (full commands are in the comment block at the
bottom of `livekit_agent.py`), and call your Twilio number.

---

## Where to put keys in Colab (only if experimenting with Part B in Colab)
Use Colab **Secrets** (🔑 icon in the left sidebar), not plain text:
```python
from google.colab import userdata
import os
os.environ["LIVEKIT_API_KEY"] = userdata.get("LIVEKIT_API_KEY")
```
Never paste secrets into a shared/downloadable notebook cell.
