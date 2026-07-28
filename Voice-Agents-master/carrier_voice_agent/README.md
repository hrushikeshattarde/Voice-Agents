# LaneVoice — In-House Carrier-Sales Voice AI

An inbound carrier-sales voice agent: it answers a phone call, identifies the
load, verifies the carrier, negotiates the rate, and **books / warm-transfers /
declines** — with a full audit trail.

**Stack:** LiveKit (telephony + SIP) · **Groq** for STT / LLM / TTS ·
SQLite + typed, deterministic Python business logic.

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
├── voice/                 # GroqTTS + GroqPhraser
├── telephony/             # LiveKit worker (STT plugin + TTS adapter + lifecycle)
└── demo.py                # text-mode simulation (no keys)
tests/                     # pytest: parsing, negotiation, verification, conversation
docs/                      # LIVE_SETUP.md, TEST_CALL_SCRIPTS.md
sip_setup/                 # LiveKit inbound-trunk + dispatch-rule JSON
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
cp .env.example .env  # fill in LiveKit + Groq keys
make worker           # uv run lanevoice-worker dev
```
SIP wiring: [docs/LIVE_SETUP.md](docs/LIVE_SETUP.md) ·
call scripts: [docs/TEST_CALL_SCRIPTS.md](docs/TEST_CALL_SCRIPTS.md).

Console entry points (installed by `uv sync`):
`lanevoice-worker` · `lanevoice-demo` · `lanevoice-initdb`.

**Full run guide (every command + every case):** [docs/USAGE.md](docs/USAGE.md).

---

## Configuration

Everything is in [src/lanevoice/settings.py](src/lanevoice/settings.py), overridable
via `.env`:

| Setting | Env var | Default |
|---|---|---|
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

`make demo` and `make test` need neither.

## Deploy
```bash
make docker           # build image
# run on any host with outbound internet (no inbound ports needed):
docker run --env-file .env lanevoice:latest
```

## Out of scope (v1)
Outbound calling, multi-load calls, live Transport Pro integration (seed DB
stands in for the load mirror), production fraud scoring. Carrier verification is
a **mock** you replace with FMCSA + a commercial fallback — the decision logic
stays.
