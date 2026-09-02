# Going Live — Twilio + LiveKit runbook

You have your Twilio and LiveKit secrets. This gets a real phone call answered
by the agent. It has three parts:

- **A.** Put secrets in `.env`
- **B.** Wire the phone number:  Twilio  →  LiveKit SIP
- **C.** Run the worker and call in

> **Where do the secrets go?** In a `.env` file next to the code (Part A) — **not**
> in Colab. Colab was only for testing the logic/voice. Live calls need a running
> worker process (Part C).

> **Where does the worker run?** Anywhere with Python + internet. It connects
> *out* to LiveKit, so **no ports to open**. Two choices:
> - **First test:** your own PC (CPU) — works, just laggy while models load.
> - **Real use:** a small Linux **GPU** VM (RunPod / Lambda / GCP / AWS g5) for
>   low latency. Same steps either way.

---

## Part A — Put your secrets in `.env`

1. In the `carrier_voice_agent` folder, copy `.env.example` to `.env`.
2. Paste your real values:
   - `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (LiveKit → project → Settings → Keys)
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`

That's it — the worker loads `.env` automatically.

---

## Part B — Wire the phone number (one-time)

### B1. Install the LiveKit CLI (`lk`) and log it in
```bash
# macOS/Linux
curl -sSL https://get.livekit.io/cli | bash
# Windows: download from https://github.com/livekit/livekit-cli/releases

lk cloud auth            # opens a browser, links the CLI to your project
```

### B2. Enable SIP on your LiveKit project
In the LiveKit dashboard → **Telephony / SIP** → enable it. Note the **SIP URI**
it gives you (looks like `sip:xxxxxxxx.sip.livekit.cloud`). You'll paste this
into Twilio in B4.

### B3. Create the inbound trunk + dispatch rule (routes calls to your agent)
Edit `sip_setup/inbound-trunk.json` and put your Twilio number in `numbers`, then:
```bash
lk sip inbound create sip_setup/inbound-trunk.json
lk sip dispatch create sip_setup/dispatch-rule.json
```
The dispatch rule drops each inbound call into a `call-…` room. Your worker
(Part C) has no fixed agent name, so it **auto-joins** those rooms and answers.

> `lk` subcommands change occasionally — if one errors, run `lk sip --help`.

### B4. Point Twilio at LiveKit
In the Twilio Console:
1. **Phone Numbers → Manage → Active numbers** — confirm you own the number.
2. **Elastic SIP Trunking → Trunks → Create a SIP Trunk.**
3. Under **Origination**, add an Origination URI = your LiveKit **SIP URI** from B2
   (e.g. `sip:xxxxxxxx.sip.livekit.cloud`).
4. Under **Numbers**, attach your Twilio phone number to this trunk.
5. Save.

Now: caller dials your Twilio number → Twilio sends it to LiveKit SIP → LiveKit
puts it in a `call-…` room → your worker answers.

---

## Part C — Run the worker

### C1. Install dependencies (on the machine that will run the worker)
```bash
uv sync
```
No local ML models — STT/LLM/TTS all run on OpenRouter, so there's nothing heavy
to download and no GPU needed.

### C2. Initialize the database once
```bash
uv run lanevoice-initdb
```

### C3. Start the worker
```bash
uv run lanevoice-worker dev     # dev mode, verbose logs — use this first
```
You should see it connect to LiveKit and wait for jobs. (If it prints "Missing
required env vars", your `.env` isn't filled in — see Part A.)

For production later:
```bash
uv run lanevoice-worker start
```

### C4. Call your number ☎️
The agent greets you, asks for a load, verifies your MC/DOT, and negotiates.
Watch the terminal for the live transcript; the call is logged to `carrier_agent.db`.

---

## Quick sanity checklist
- [ ] `.env` filled with real LiveKit + Twilio values
- [ ] SIP enabled on LiveKit; SIP URI copied
- [ ] `lk sip inbound create` + `lk sip dispatch create` succeeded
- [ ] Twilio SIP trunk Origination URI = LiveKit SIP URI; number attached
- [ ] `uv run lanevoice-worker dev` is running and connected
- [ ] Dial the number → agent answers

## If the call connects but the agent is silent / errors
- **Speech runs on LiveKit Inference by default** (`STT_PROVIDER` /
  `TTS_PROVIDER=inference`): streaming transcription and a streaming voice on
  the LiveKit credentials, no OpenRouter involved. At startup the worker
  composes the greeting once and renders it and the filler clips in the
  configured voice, so a wrong `TTS_INFERENCE_MODEL` or `TTS_INFERENCE_VOICE`
  shows up in the prewarm log as `clip ... failed to synthesize` with the
  gateway's error, and the worker carries on (greeting composed live, no
  fillers) rather than dying. Look for `greeting rendered:` and `dead-air
  fillers ready:` in the startup log — both present means the voice works. If
  Inference is not enabled on the LiveKit project, set both providers to
  `openrouter` to get the previous pipeline back.
- **Silent / TTS error on `TTS_PROVIDER=openrouter`:** the worker synthesises
  one warm-up phrase at startup, so a bad `TTS_MODEL` or a `TTS_VOICE` the model
  doesn't offer fails there with the HTTP status in the log rather than as
  silence on the call. Use the default `microsoft/mai-voice-2-flash` with
  `TTS_VOICE=en-US-Ethan:MAI-Voice-2`. Do NOT leave `TTS_VOICE` empty on a model
  that has no stable default — fish-audio picks a different speaker per request,
  so the agent changes voice mid-call.
- **Agent never hears the caller:** with `STT_PROVIDER=inference` the log shows
  `stt: inference / assemblyai/universal-streaming` at startup and `CALLER said →`
  lines as the caller talks. No caller lines at all points at the Inference STT connection
  (check the LiveKit project has Inference enabled) or at the VAD thresholds in
  `settings.py`.
- **Dead air / no answer:** the worker isn't picking up the room — confirm it's
  running and that the dispatch rule was created (`lk sip dispatch list`).
- **`Missing required env vars`:** fill `.env` (LiveKit + `OPENROUTER_API_KEY`).

## Cost note
LiveKit free tier + Twilio per-minute apply. The HF models are free. A cloud GPU
VM is the only real recurring cost for good latency — spin it down when not testing.
