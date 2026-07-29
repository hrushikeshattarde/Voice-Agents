# LaneVoice — In-House Carrier-Sales Voice AI

An inbound carrier-sales voice agent: it answers a phone call, identifies the
load, verifies the carrier, negotiates the rate, and **books / warm-transfers /
declines** — with a full audit trail.

**Stack:** LiveKit (telephony + SIP) · **Groq** for STT / LLM / TTS ·
**Transport Pro** Public API as the system of record · typed, deterministic
Python business logic.

---

## Project layout

```
src/lanevoice/
├── settings.py            # all config (env-driven, typed) — one place to change models
├── logging_config.py
├── parsing.py             # extract load IDs / MC-DOT / money from utterances
├── domain/                # typed models + enums (Load, Carrier, NegotiationResult, ...)
├── db/                    # Database (schema/seed) + Repository (typed data access)
├── services/              # the deterministic "product": loads, verification,
│                          #   negotiation engine, transfer
├── conversation/          # CarrierSalesAgent — the call state machine (the brain)
├── integrations/          # Transport Pro: client, mappers, repository, tpcheck
├── datasource.py          # picks the backend from DATA_SOURCE
├── voice/                 # GroqTTS + GroqComposer (writes every spoken turn)
├── telephony/             # LiveKit worker (STT plugin + TTS adapter + lifecycle)
│                          #   + whisper.py: rings the rep, briefs them, press 9 to
│                          #     take the call; transfer.py: blind REFER fallback
└── demo.py                # text-mode simulation (no keys)
tests/                     # pytest: parsing, negotiation, verification, conversation,
                           #   Transport Pro client / mappers / repository / full calls,
                           #   transfer + whisper handoff
docs/                      # LIVE_SETUP.md, TEST_CALL_SCRIPTS.md, TRANSPORT_PRO.md
sip_setup/                 # LiveKit inbound + outbound trunk + dispatch-rule JSON
Dockerfile · Makefile · pyproject.toml
```

### Design guarantee (PRD §4 / §9.4)
The LLM is the **conversational interface only**. Load lookup, carrier
verification, offer accept/reject vs. the ceiling, book, and transfer are all
deterministic Python in `services/`. A caller **cannot talk the model into a bad
outcome** because the model has no authority to cause one — proven by the unit
tests in `tests/`.

---

## Quick start

```bash
make install          # uv sync --extra dev
make test             # 18 unit tests, no keys needed
make demo             # text simulation of every scenario
```

To take real calls, add credentials then run the worker:
```bash
cp .env.example .env  # fill in LiveKit + Groq + Transport Pro
make worker           # uv run lanevoice-worker dev
```
`make demo` and `make test` run entirely offline on the seed data
(`DATA_SOURCE=sqlite`) and need no credentials at all.

### Checking it pulls the right data

Three commands, cheapest first — no phone needed for any of them.

```bash
lanevoice-tpcheck --load 2520571 --mc 343195 --raw
```
Read-only. Authenticates, fetches that load and that carrier, and prints the raw
payload beside what the mappers made of it — status, `isPosted`, the floor and
max buy, the lane, the notes, the addresses on the account. When something won't
work it names the setting or the constant to change.

```bash
lanevoice-demo --chat --live --facts
```
The whole call in the terminal: you type as the carrier, against the **real**
board, through the same repository the phone worker uses. `--facts` prints the
data behind every turn — the only load, carrier and rate values the agent was
allowed to speak. Drop `--live` to run the same thing on seed data.

> `--live` posts a real offer to Transport Pro if you take a booking all the way
> through. Use a test load if that matters.

```bash
make test
```
257 tests, offline. Includes the two real production load payloads.
SIP wiring: [docs/LIVE_SETUP.md](docs/LIVE_SETUP.md) ·
call scripts: [docs/TEST_CALL_SCRIPTS.md](docs/TEST_CALL_SCRIPTS.md).

Console entry points (installed by `uv sync`):
`lanevoice-worker` · `lanevoice-demo` · `lanevoice-initdb` · `lanevoice-tpcheck`.

**Full run guide (every command + every case):** [docs/USAGE.md](docs/USAGE.md).

---

## Configuration

Everything is in [src/lanevoice/settings.py](src/lanevoice/settings.py), overridable
via `.env`:

| Setting | Env var | Default |
|---|---|---|
| **Data source** | `DATA_SOURCE` | `transportpro` (or `sqlite` for offline) |
| Transport Pro API root | `TRANSPORT_PRO_URL` | — (required) |
| Transport Pro login | `TRANSPORT_PRO_USERNAME` / `_PASSWORD` | — (required) |
| **Load statuses the agent sells** | `TRANSPORT_PRO_OPEN_LOAD_STATUSES` | `ready to dispatch` |
| API timeout (s) | `TRANSPORT_PRO_TIMEOUT` | `10.0` |
| Fraud tripwire, share of board rate | `TRANSPORT_PRO_FRAUD_LOW_RATIO` | `0.5` |
| Load numbers read aloud on a miss | `TRANSPORT_PRO_MAX_OFFERED_LOADS` | `5` |
| STT model | `STT_MODEL` | `whisper-large-v3-turbo` |
| LLM model | `LLM_MODEL` | `llama-3.1-8b-instant` |
| TTS model / voice | `TTS_MODEL` / `TTS_VOICE` | `canopylabs/orpheus-v1-english` / `troy` |
| Phrase via LLM? | `USE_LLM` | `false` (fast templates) |
| Turn buffer (s) | `MIN_ENDPOINTING_DELAY` | `0.8` |
| Negotiation rounds | `MAX_NEGOTIATION_ROUNDS` | `8` |
| Reserve below Max Buy | `NEGOTIATION_BUFFER` | `0` (may reach Max Buy) |
| Share of their move we return | `NEGOTIATION_RECIPROCITY` | `0.5` (lower = firmer) |
| Agent's own authority | `NEGOTIATION_DISCRETION_RATE` | `0.6` of floor→Max Buy |
| Gap not worth haggling | `NEGOTIATION_SETTLE_GAP_RATE` | `0.10` |
| Gap that triggers the split close | `NEGOTIATION_SPLIT_GAP_RATE` | `0.30` |
| Best-and-final if they never moved | `NEGOTIATION_STONEWALL_FINAL_RATE` | `0.5` |
| Pushes before best-and-final | `NEGOTIATION_MAX_HOLDS` | `2` |

Change a model = change one line (or one env var). Nothing else hard-codes it.

## API keys

| Service | Vars | Purpose |
|---|---|---|
| **LiveKit** ([cloud.livekit.io](https://cloud.livekit.io)) | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | telephony + SIP + phone number |
| **Groq** ([console.groq.com](https://console.groq.com), free tier) | `GROQ_API_KEY` | STT + LLM + TTS |
| **Transport Pro** Public API | `TRANSPORT_PRO_URL`, `TRANSPORT_PRO_USERNAME`, `TRANSPORT_PRO_PASSWORD` | loads, carrier vetting, contacts, offers |

`make demo` and `make test` need none of them.

## Deploy
```bash
make docker           # build image
# run on any host with outbound internet (no inbound ports needed):
docker run --env-file .env lanevoice:latest
```

## Transport Pro integration

Loads, carrier vetting and contact addresses come from the Transport Pro Public
API; the **call audit trail stays local** (the API has no endpoint for it, and
losing it would make a disputed booking unauditable).

See **[docs/TRANSPORT_PRO.md](docs/TRANSPORT_PRO.md)** for the endpoint map, the
field mapping, the three call gates, and the two things to verify against live
data before go-live.

## Out of scope (v1)
Outbound calling, multi-load calls, production fraud scoring beyond the
board-rate tripwire. FMCSA is not consulted directly — carrier vetting is
whatever Transport Pro reports for the MC/USDOT.
