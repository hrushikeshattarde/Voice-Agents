# USAGE — running LaneVoice, every case

A single reference for every way you'll run this project: local testing (no
keys), the live phone agent, Docker, changing models, inspecting data, and
troubleshooting. Commands assume you're in the `carrier_voice_agent/` folder.

Every task has a **Make target** and the **raw command** it runs — use whichever
you prefer (`make` isn't required).

---

## 0. Prerequisites

| Tool | Why | Check |
|---|---|---|
| **Python 3.11 or 3.12** | 3.13/3.14 lack some wheels | `python --version` |
| **uv** | env + dependency manager | `uv --version` |
| **git** | version control | `git --version` |
| **make** (optional) | shortcut targets | `make --version` |

> Install uv: `pipx install uv` (or see astral.sh/uv). On Windows, `make` isn't
> built in — just use the raw `uv run …` commands shown under each target.

---

## 1. Install

```bash
make install
# raw:
uv sync --extra dev
```
Creates `.venv`, installs runtime + dev dependencies, and installs the
`lanevoice` package with its console scripts (`lanevoice-worker`,
`lanevoice-demo`, `lanevoice-initdb`).

---

## 2. Case A — Try it with NO keys (text simulation)

The fastest way to see the whole flow. No LiveKit, no Groq, no models.

```bash
make demo
# raw:
uv run lanevoice-demo
```
Runs five scripted scenarios (book, walk-up, no-deal, fraud, revoked).

Interactive — you play the carrier:
```bash
uv run lanevoice-demo --chat
```
Type carrier lines; `Ctrl+C` / `Ctrl+Z` to stop. Try:
`L1001` → `MC 123456` → `2100` → `deal`.

---

## 3. Case B — Run the tests / quality checks

```bash
make test          # uv run pytest -q
make lint          # uv run ruff check src tests
make fmt           # uv run ruff check --fix src tests
```
18 unit tests cover parsing, the negotiation engine (including the "never pay
above the cap" safety rule), verification, and end-to-end conversation outcomes.
None require keys.

Run one test file / one test:
```bash
uv run pytest tests/test_negotiation.py -v
uv run pytest -k "no_deal" -v
```

---

## 4. Case C — Run the LIVE phone agent

### 4.1 Configure credentials
```bash
cp .env.example .env
```
Fill in (see [table below](#7-configuration--changing-models)):
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
- `GROQ_API_KEY`

### 4.2 TTS model
Defaults to `canopylabs/orpheus-v1-english` (Groq's Orpheus). The old
`playai-tts` model was shut down 2025-12-31 — no terms to accept. Override the
model/voice via `TTS_MODEL` / `TTS_VOICE` in `.env` if you like (voices: `troy`,
`autumn`, `diana`, `hannah`, `austin`, `daniel`).

### 4.3 One-time: wire the phone number (SIP)
Follow [docs/LIVE_SETUP.md](LIVE_SETUP.md) — create a LiveKit inbound trunk +
dispatch rule for your number. (No Twilio needed if you got the number from
LiveKit.)

### 4.4 Initialize the database (first run only)
```bash
make initdb        # uv run lanevoice-initdb
```

### 4.5 Start the worker
```bash
make worker        # uv run lanevoice-worker dev   (verbose, use first)
# production:
uv run lanevoice-worker start
```
You should see `registered worker … livekit.cloud`, then `STT/TTS` loaded on
prewarm. Leave it running and **call your number**.

Use [docs/TEST_CALL_SCRIPTS.md](TEST_CALL_SCRIPTS.md) to exercise every branch by
voice.

---

## 5. Case D — Inspect what happened (audit trail)

After calls, read the SQLite audit trail:
```bash
uv run python -c "import sqlite3; c=sqlite3.connect('carrier_agent.db'); c.row_factory=sqlite3.Row; \
[print(dict(r)) for r in c.execute('SELECT call_id,outcome,load_id,carrier_dot FROM calls')]; \
print('--- offers ---'); [print(dict(r)) for r in c.execute('SELECT call_id,round_number,offered_by,amount FROM negotiation_offers ORDER BY id')]; \
print('--- notes ---'); [print(r['note']) for r in c.execute('SELECT note FROM call_notes')]"
```
Or open `carrier_agent.db` in any SQLite browser. Tables: `calls`,
`negotiation_offers`, `transfer_events`, `call_notes`, `loads`, `carriers`, `reps`.

Reset the seed data (fresh loads/carriers):
```bash
uv run python -c "from lanevoice.db import Database; from lanevoice.settings import get_settings; Database(get_settings().db_path).reset(seed=True); print('reset')"
```

---

## 6. Case E — Deploy with Docker

```bash
make docker                 # docker build -t lanevoice:latest .
docker run --env-file .env lanevoice:latest
```
The worker connects **out** to LiveKit, so no inbound ports are needed. Run it on
any always-on host (a small Linux VM). GPU is not required — STT/LLM/TTS run on
Groq.

---

## 7. Configuration & changing models

Everything lives in [src/lanevoice/settings.py](../src/lanevoice/settings.py) and
is overridable via `.env` (env var wins). **Change a model = change one value.**

| Setting | Env var | Default |
|---|---|---|
| LiveKit URL / key / secret | `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | — (required for live) |
| Groq key | `GROQ_API_KEY` | — (required for live) |
| STT model | `STT_MODEL` | `whisper-large-v3-turbo` |
| LLM model | `LLM_MODEL` | `llama-3.1-8b-instant` |
| TTS model / voice | `TTS_MODEL` / `TTS_VOICE` | `canopylabs/orpheus-v1-english` / `troy` |
| Phrase via LLM? | `USE_LLM` | `false` (fast templates) |
| Turn buffer (sec) | `MIN_ENDPOINTING_DELAY` / `MAX_ENDPOINTING_DELAY` | `0.8` / `8.0` |
| Negotiation rounds | `MAX_NEGOTIATION_ROUNDS` | `8` |
| Reserve held below Max Buy | `NEGOTIATION_BUFFER` | `0` (may reach Max Buy) |
| Share of their move we give back | `NEGOTIATION_RECIPROCITY` | `0.5` (lower = firmer) |
| How far the bot commits alone | `NEGOTIATION_DISCRETION_RATE` | `0.6` of floor→Max Buy |
| Gap not worth haggling over | `NEGOTIATION_SETTLE_GAP_RATE` | `0.10` |
| Gap that triggers the split close | `NEGOTIATION_SPLIT_GAP_RATE` | `0.30` |
| Best-and-final if they never moved | `NEGOTIATION_STONEWALL_FINAL_RATE` | `0.5` |
| Pushes before best-and-final | `NEGOTIATION_MAX_HOLDS` | `2` |
| DB path | `DB_PATH` | `carrier_agent.db` |
| Log level | `LOG_LEVEL` | `INFO` |

Examples:
```bash
# smarter phrasing:
echo "USE_LLM=1" >> .env
echo "LLM_MODEL=llama-3.3-70b-versatile" >> .env
# a different TTS voice:
echo "TTS_VOICE=autumn" >> .env
```

---

## 8. Troubleshooting — every common case

| Symptom | Cause | Fix |
|---|---|---|
| `Missing required settings: …` on start | `.env` not filled | Add LiveKit + `GROQ_API_KEY` to `.env` |
| Worker starts, call connects, **silent** | wrong/removed `TTS_MODEL` (e.g. old `playai-tts`) | Use `canopylabs/orpheus-v1-english` (default) |
| Call connects but **agent never joins** | dispatch rule missing | `lk sip dispatch list`; recreate per LIVE_SETUP §B3 |
| Call **fails to connect** | number not attached / SIP URI mismatch | Recheck LIVE_SETUP §B |
| STT hears wrong **digits** ("L1001"→"anyone") | phone audio + model | Say digits slowly, one at a time |
| `uv sync` tries to compile / fails | Python 3.13/3.14 | `uv venv --python 3.12 && uv sync` |
| `GROQ_API_KEY` errors / 401 | bad/rotated key | Regenerate in Groq console, update `.env` |
| Two workers answer oddly | more than one worker registered | Run only one `lanevoice-worker` at a time |
| Import errors running `python foo.py` | package not on path | Use `uv run …` (or `uv sync` first) |

Turn up logs for a call:
```bash
echo "LOG_LEVEL=DEBUG" >> .env
```

---

## 9. Command cheat-sheet

| Goal | Make | Raw |
|---|---|---|
| Install | `make install` | `uv sync --extra dev` |
| Text demo | `make demo` | `uv run lanevoice-demo` |
| Interactive demo | — | `uv run lanevoice-demo --chat` |
| Tests | `make test` | `uv run pytest -q` |
| Lint / format | `make lint` / `make fmt` | `uv run ruff check [--fix] src tests` |
| Init DB | `make initdb` | `uv run lanevoice-initdb` |
| Worker (dev) | `make worker` | `uv run lanevoice-worker dev` |
| Worker (prod) | — | `uv run lanevoice-worker start` |
| Build image | `make docker` | `docker build -t lanevoice:latest .` |
