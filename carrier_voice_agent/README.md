# LaneVoice — In-House Carrier-Sales Voice AI (simple build)

A minimal, open-source implementation of the PRD's inbound carrier-sales flow:
**identify load → verify carrier → negotiate rate (open low, walk up, never above the ceiling) → book, warm-transfer, or decline**,
with a full audit trail in a database.

Built to run on **Google Colab** with **free Hugging Face models** and no paid
APIs for testing. Real phone calls use **LiveKit + Twilio** (deployment scaffold included).

---

## What's in here

| File | Role |
|---|---|
| `database.py` | SQLite data layer — loads, carriers, reps, calls, offers, transfers (+ seed data) |
| `business_logic.py` | **The deterministic "product"** — load lookup, carrier verify (mock FMCSA), the **server-side negotiation engine** (opens low, walks up ~$25–30/round, hard cap at ceiling−150, no-deal above ceiling), rep/transfer resolution |
| `conversation.py` | Call **state machine** (PRD §3). LLM only *phrases*; it never decides prices |
| `voice_pipeline.py` | Free HF models: **Whisper** (STT) + **Qwen2.5** (phrasing) + **Kokoro** (TTS) |
| `run_demo.py` | Text-mode simulation — verify all logic with **zero models / zero keys** |
| `livekit_agent.py` | **Deployment scaffold**: same brain wired to LiveKit Agents + Twilio SIP |
| `Voice_AI_Carrier_Agent_Colab.ipynb` | One-click Colab notebook (setup → text demo → voice demo) |

### Design guarantee (PRD §4 / §9.4)
The LLM is the **conversational interface only**. Every consequential decision —
is this MC valid, is this offer acceptable, book or transfer — is plain Python in
`business_logic.py`, validated server-side against the live ceiling. A caller
**cannot talk the model above the ceiling** because the model has no authority to book.

---

## Quick start (Colab — 0 API keys)

1. Upload this folder to Colab (or open `Voice_AI_Carrier_Agent_Colab.ipynb`).
2. Runtime → Change runtime type → **GPU** (T4 is fine; CPU works but slower).
3. Run the cells top to bottom:
   - **Text demo** runs instantly (no models).
   - **Voice demo** downloads the free HF models, then you type a carrier line and
     *hear* the agent reply; or upload/record a short WAV to test real STT.

Quick local check (no models needed):
```bash
python run_demo.py            # scripted scenarios
python run_demo.py --chat     # you play the carrier
```

---

## API keys — what you need, and when

### To test everything in Colab (text + local voice): **NONE.**
Whisper, Qwen2.5, and Kokoro are all free, ungated, and run locally.

### To take real phone calls (LiveKit + Twilio deployment):

| Service | Keys / values | Why | Cost |
|---|---|---|---|
| **LiveKit** (open source; Cloud has a free tier) | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Media transport + SIP bridge that your agent worker joins | Free tier, then usage-based |
| **Twilio** | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, a **Voice phone number**, an **Elastic SIP Trunk** | PSTN — the actual phone number carriers dial | Pay per number + per-minute |
| **Hugging Face** (optional) | `HF_TOKEN` | Only if you swap in a **gated** model (e.g. Llama). Qwen/Whisper/Kokoro need no token | Free |
| **FMCSA QCMobile** (optional, for *real* carrier verification) | `FMCSA_WEBKEY` | Replace the mock `verify_carrier()` with live authority/insurance data | Free (requires Login.gov) |
| **ngrok** (optional) | `NGROK_AUTHTOKEN` | Only if you expose a webhook from Colab for experiments | Free tier |

> The LiveKit worker dials **out** to LiveKit Cloud, so the GPU host needs **no inbound
> ports** — that's why real calls run on a small GPU box, not Colab.

Set them as environment variables (or Colab secrets):
```bash
export LIVEKIT_URL="wss://<project>.livekit.cloud"
export LIVEKIT_API_KEY="..."
export LIVEKIT_API_SECRET="..."
export TWILIO_ACCOUNT_SID="..."
export TWILIO_AUTH_TOKEN="..."
```

---

## From demo to real calls (later)

1. Get the logic right in Colab (text + voice demo).
2. Provision LiveKit Cloud + a Twilio number & SIP trunk (steps at the bottom of `livekit_agent.py`).
3. Deploy `livekit_agent.py` on a small GPU host: `python livekit_agent.py start`.
4. Call your Twilio number.

## Deliberately out of scope for v1 (per PRD non-goals)
Outbound calling, multi-load calls, live Transport Pro integration (seed DB stands in
for the load mirror), and production-grade fraud scoring. Verification here is a **mock**
you replace with FMCSA + a commercial fallback (PRD §8.2).
