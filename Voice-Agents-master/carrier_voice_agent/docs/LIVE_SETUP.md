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

### B5. Create an OUTBOUND trunk (needed for the warm handoff)

The inbound trunk lets carriers reach you. The warm transfer works the other way:
when a caller **asks for a person**, the agent rings the rep, tells them who is
waiting and why, and joins them to the carrier only once they press 9. That means
placing an outbound call.

> Only an explicit request is transferred. Everything else the agent can't finish
> becomes a callback — the carrier is told a rep will ring them, and nobody's phone
> rings mid-call. So this trunk is on the path for "can I talk to the sales rep",
> not for every handoff.

> **LiveKit does not carry calls to the phone network.** It is the SIP *client*, so
> an outbound trunk's **Address** is always somebody else's SIP host — the thing
> that actually rings a phone. You cannot reach a rep's mobile with LiveKit alone.
> Three things that host can be:
>
> | Address points at | When to use it | Extensions |
> |---|---|---|
> | **Twilio Termination** (`your-trunk.pstn.twilio.com`) | you already have the inbound trunk there | `WHISPER_EXTENSION_MODE=dtmf` |
> | **Another SIP carrier** (Telnyx, Bandwidth, …) | cheaper per-minute at volume | `dtmf` |
> | **Your own PBX / phone system** | reps are already on SIP desk phones | `sip_user` — cleanest |

**Using Twilio as the carrier.** In the Twilio Console, on the same Elastic SIP
Trunk, under **Termination**:
1. Set a **Termination SIP URI** — note the hostname (e.g. `your-trunk.pstn.twilio.com`).
2. Add a **Credential List** (username + password) and attach it.
3. Under **Voice → Settings**, switch on **"Enable PSTN Transfer"** and set
   **Transfer Caller ID** to the number you want the rep to see. This one is also
   what a blind transfer needs, so set it either way.

**Then create the trunk in LiveKit.** Either the dashboard form
(**Telephony → Create a new trunk → Outbound**) or the CLI — they write the same
record:

| Dashboard field | JSON / API | What to put |
|---|---|---|
| Trunk name | `name` | anything, e.g. `carrier-sales-outbound` |
| Trunk direction | — | **Outbound** |
| Address | `address` | the carrier's SIP host, **no `sip:` prefix**, no path |
| Transport | `transport` | `AUTO` unless your carrier demands one; `TLS` to encrypt signalling |
| Numbers | `numbers` | the caller ID the **rep** sees — a number you own on that trunk, E.164 |
| Optional → Username | `auth_username` | the carrier's termination credentials… |
| Optional → Password | `auth_password` | …or leave both out if they use an IP allow-list |

```bash
cp sip_setup/outbound-trunk.json /tmp/outbound.json   # fill in address + auth first
lk sip outbound create /tmp/outbound.json
```
Put the id it prints (`ST_…`) into `.env` as `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`.

**Then decide how the extension gets dialled.** Transport Pro gives the rep as
`312-300-7447 ext8754`, and the base number alone reaches a switchboard. Set
`WHISPER_EXTENSION_MODE` to match what answers your calls (`dtmf` / `sip_user` /
`off` — see `.env.example`). If your reps have direct DIDs, put those in Transport
Pro and none of this matters.

> Without it, `lanevoice-worker` warns at startup and every handoff fails rather
> than falling back. Set `WHISPER_ENABLED=0` for a blind transfer instead, or
> `SIP_TRANSFER_ENABLED=0` to only announce and log handoffs.

Two things to know before the first live handoff:

* **The rep's number comes from Transport Pro**, off the load's carrier-rep
  contact. Check it resolves before you rely on it:
  `uv run lanevoice-tpcheck --load <id>` prints who a caller asking for the rep
  would reach, and says so plainly if the answer is "whoever is free".
* **Keep the `reps` table current** (`uv run lanevoice-initdb` seeds examples).
  That table is the fallback when a load has no carrier rep assigned, or theirs has
  no number on file, and every row is a real phone number that will actually ring.

### B6. Tell your reps about the keypress

A rep who doesn't know to press **9** is a rep who listens to the briefing, says
"hello?", and gets told the carrier will be called back. One line is enough:
*"the AI will call you, tell you who's holding, and you press 9 to take it — or 1 to
hear it again."*

`WHISPER_ACCEPT_DIGIT` and `WHISPER_REPEAT_DIGIT` change the keys if your desk
already has a convention.

---

## Part C — Run the worker

### C1. Install dependencies (on the machine that will run the worker)
```bash
uv sync
```
No local ML models — STT/LLM/TTS all run on Groq, so there's nothing heavy to
download and no GPU needed.

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
- [ ] Termination URI + credentials set; "Enable PSTN Transfer" on
- [ ] `lk sip outbound create` done; `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` in `.env`
- [ ] `reps` table holds real numbers (the fallback when a load names no rep)
- [ ] Reps know to press 9
- [ ] `uv run lanevoice-worker dev` is running and connected
- [ ] Dial the number → agent answers
- [ ] Ask for the sales rep → your phone rings, briefs you, and 9 connects you

## If the call connects but the agent is silent / errors
- **Silent / TTS error:** the old `playai-tts` model was shut down 2025-12-31.
  Use the default `canopylabs/orpheus-v1-english`, or set another `TTS_MODEL` in `.env`.
- **Dead air / no answer:** the worker isn't picking up the room — confirm it's
  running and that the dispatch rule was created (`lk sip dispatch list`).
- **`Missing required env vars`:** fill `.env` (LiveKit + `GROQ_API_KEY`).
- **"Putting you through…" and then it apologises:** the handoff was refused. The
  worker logs the reason — usually a missing `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`, PSTN
  transfer not enabled on the trunk (B5), or a rep number that isn't dialable.
- **The rep's phone rings but they hear nothing:** the briefing is TTS, so a Groq
  TTS failure is silence. The worker logs the whisper script it rendered — check it
  appears, then check `TTS_MODEL`.
- **Carriers keep being told the rep will call back:** the rep isn't pressing 9
  within `WHISPER_DECISION_SECONDS`. Usually reps who haven't been told about the
  keypress (B6), or voicemail answering — which is *supposed* to end in a callback.
  Check the load notes for `OWES THIS CARRIER A CALL`; somebody has to make those.
- **Long silence for the carrier while a rep is found:** expected, and bounded. The
  briefing happens in a room the carrier isn't in, so they hear nothing until the
  rep accepts. `WHISPER_REASSURE_AFTER` controls when the agent speaks to them.

## Cost note
LiveKit free tier + Twilio per-minute apply. The HF models are free. A cloud GPU
VM is the only real recurring cost for good latency — spin it down when not testing.
