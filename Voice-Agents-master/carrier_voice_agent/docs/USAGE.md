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
`lanevoice-demo`, `lanevoice-initdb`, `lanevoice-tpcheck`,
`lanevoice-dashboard`).

---

## 2. Case A — Try it with NO keys (text simulation)

The fastest way to see the whole flow. No LiveKit, no OpenRouter, no models.

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
713 tests cover parsing, the negotiation engine (including the "never pay
above the cap" safety rule), verification, end-to-end conversation outcomes,
the Transport Pro wire format, and the practice trainer. None require keys —
a filled-in `.env` is deliberately neutralized so every machine runs the same
suite (see `tests/conftest.py`).

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
Fill in (see [table below](#8-configuration--changing-models)):
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
- `OPENROUTER_API_KEY` — one key covers STT, the LLM and TTS

### 4.2 TTS model
Defaults to `microsoft/mai-voice-2-flash` with the stable named voice
`en-US-Ethan:MAI-Voice-2` — chosen by measurement (the table lives in
`settings.py`): fastest of the candidates at ~0.12× real time, and it keeps
**one voice across turns**. Fish Audio, the previous default, rejects the
`voice` parameter outright and its provider-side fallback voice changed
between turns of a single call — heard live.

Voices are namespaced to the TTS provider (an OpenAI name like `alloy` means
nothing to Microsoft). To compare candidates by ear and latency:
```bash
uv run python tools/audition_voices.py
```

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

The web dashboard is the comfortable way — a HappyRobot-style UI over the same
database:
```bash
make dashboard
# raw:
uv run lanevoice-dashboard          # http://127.0.0.1:8710
```
- **Overview** — booking rate, calls per day, outcome split.
- **Runs** — every call with its transcript, the negotiation ladder round by
  round, and the notes/transfer timeline. The transcript streams **live during
  the call** (the agent persists it turn by turn) — an in-progress call shows a
  pulsing "Live" chip and its drawer follows the conversation as it happens.
  Playground test calls are labelled so they can't be mistaken for phone calls.
  With `RECORD_CALLS=true` (and the worker restarted), each finished phone call
  also carries a **playable recording** in its drawer — read the consent and
  retention notes on `RECORD_CALLS` in `settings.py` before enabling, and add a
  "this call may be recorded" line to the greeting.
- **Loads** — the local board the offline playground sells against.
- **Playground** — drive the REAL `CarrierSalesAgent` by text in the browser:
  same repository, same negotiation engine, same audit trail as the phone line.
  Offline it uses the seed board; "Start live call" honours `DATA_SOURCE` and
  behaves exactly like `lanevoice-demo --chat --live` (a completed booking posts
  a real offer).
- **Practice** — the trainer: reps pitch simulated customers and get scored.
  Its own case below ([Case F](#7-case-f--practice-a-sales-pitch-the-trainer)).
- **Settings** — the models, knobs and integrations currently in force
  (key *presence* only; secrets never reach the browser).

No extra dependencies and no keys needed for the offline parts — stdlib HTTP
only, bound to 127.0.0.1 by default. The dashboard does **not** answer the
phone; that is `lanevoice-worker`'s job, and both can run at the same time
against the same database.

Or read the SQLite audit trail by hand:
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
any always-on host (a small Linux VM). GPU is not required — STT/LLM/TTS all run
on OpenRouter.

---

## 7. Case F — Practice a sales pitch (the trainer)

The dashboard's **Practice** tab flips the table: the model plays a CUSTOMER in
one of eight moods (the Brush-off, the Rate Shopper, the Burned Shipper, …) and
the rep makes the pitch — **by voice** (hold the talk button, speak, release)
or by text. Every finished session is scored on a fixed conversation rubric,
voice sessions additionally get a vocal-delivery verdict (confidence, clarity,
energy, pace, warmth) plus hard metrics (talk ratio, fillers/min, pauses), the
whole call is stitched into a replayable recording, and the report can be
emailed to the rep's account manager.

```bash
make dashboard
# raw:
uv run lanevoice-dashboard          # then open http://127.0.0.1:8710/#/practice
```

Requirements by feature — everything degrades honestly when unset:

| Feature | Needs | Without it |
|---|---|---|
| The customer + the judge | `OPENROUTER_API_KEY` (and `USE_LLM` not `false`) | starting a session is a 400 that names the setting |
| Voice mode | a browser microphone (127.0.0.1 counts as a secure origin) | that session falls back to typing |
| Vocal-delivery verdict | `PRACTICE_DELIVERY_MODEL` (default is set) | conversational scorecard only |
| Report email | a manager in `managers.toml` **and** `SMTP_HOST` + `SMTP_FROM` | report stored and shown; the skip is recorded on it |

Add an account manager (restart the dashboard afterwards):
```toml
# src/lanevoice/practice/data/managers.toml
[[managers]]
id = "asmith"
name = "Alex Smith"
email = "alex.smith@circledelivers.com"
```

The eight personas live one-TOML-each in
`src/lanevoice/practice/data/profiles/` — edit a mood, an objection or a win
condition there. The loader refuses a malformed file at startup with the file
and field named, and the browser never receives the hidden facts, so a rep
can't read the answer key with devtools.

**Recordings.** Both sides of a voice session are stitched into `call.wav`
under `practice_audio/<session_id>/` (next to the DB) and served in the report
view. The raw per-turn clips are deleted the moment scoring finishes unless
`PRACTICE_KEEP_AUDIO=true` — these are recordings of your reps; keeping them
is a decision, not a default.

Past sessions: **Practice → Recent sessions** — click a row for its scorecard,
recording and transcript.

**Test scripts** — worked strong/weak plays per persona and the system checks
(silent clips, turn cap, retention, all three email states):
[PRACTICE_SCRIPTS.md](PRACTICE_SCRIPTS.md).

---

## 8. Configuration & changing models

Everything lives in [src/lanevoice/settings.py](../src/lanevoice/settings.py) and
is overridable via `.env` (env var wins). **Change a model = change one value.**

| Setting | Env var | Default |
|---|---|---|
| LiveKit URL / key / secret | `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | — (required for live) |
| OpenRouter key | `OPENROUTER_API_KEY` | — (required for live: STT + LLM + TTS) |
| STT model | `STT_MODEL` | `openai/whisper-large-v3-turbo` |
| LLM provider | `LLM_PROVIDER` | `openrouter` (or `anthropic`) |
| LLM model | `LLM_MODEL` | unset = `anthropic/claude-haiku-4.5` |
| Record phone calls? | `RECORD_CALLS` | `false` (consent + retention notes in settings.py) |
| Practice: vocal-delivery judge | `PRACTICE_DELIVERY_MODEL` | `google/gemini-3.7-flash` (empty = off) |
| Practice: keep raw voice clips? | `PRACTICE_KEEP_AUDIO` | `false` |
| Practice report email | `SMTP_HOST` / `SMTP_FROM` (+ `SMTP_USERNAME`/`SMTP_PASSWORD`) | — (unset = reports never mailed) |
| TTS model | `TTS_MODEL` | `microsoft/mai-voice-2-flash` |
| TTS voice | `TTS_VOICE` | `en-US-Ethan:MAI-Voice-2` |
| TTS timeout (sec) | `TTS_TIMEOUT` | `15.0` |
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
# compose on Claude first-party instead of through the gateway
# (still needs OPENROUTER_API_KEY for speech):
echo "LLM_PROVIDER=anthropic" >> .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
# mail practice reports to account managers:
echo "SMTP_HOST=smtp.office365.com" >> .env
echo "SMTP_FROM=lanevoice@yourcompany.com" >> .env
echo "SMTP_USERNAME=lanevoice@yourcompany.com" >> .env
echo "SMTP_PASSWORD=..." >> .env
```

---

## 9. Troubleshooting — every common case

| Symptom | Cause | Fix |
|---|---|---|
| `Missing required settings: …` on start | `.env` not filled | Add LiveKit + `OPENROUTER_API_KEY` to `.env` |
| `OpenRouter /audio/speech -> HTTP 400` at startup | `TTS_MODEL` is not a TTS model, or `TTS_VOICE` is one this model doesn't have | Use the default model; clear `TTS_VOICE` to fall back to the model's own voice |
| Voice sounds **too fast / too slow** | the response carried no rate and `TTS_SAMPLE_RATE` is wrong | Check the logged `at N Hz` line; set `TTS_SAMPLE_RATE` to match |
| Call connects, agent speaks, then **long gaps** | slow TTS model or gateway load | re-measure with `uv run python tools/measure_latency.py --tts`; the default `mai-voice-2-flash` measured ~0.12× real time |
| Call connects but **agent never joins** | dispatch rule missing | `lk sip dispatch list`; recreate per LIVE_SETUP §B3 |
| Call **fails to connect** | number not attached / SIP URI mismatch | Recheck LIVE_SETUP §B |
| STT hears wrong **digits** ("L1001"→"anyone") | phone audio + model | Say digits slowly, one at a time |
| `uv sync` tries to compile / fails | Python 3.13/3.14 | `uv venv --python 3.12 && uv sync` |
| `OPENROUTER_API_KEY` errors / 401 | bad/rotated key | Regenerate at [openrouter.ai/keys](https://openrouter.ai/keys), update `.env` |
| Two workers answer oddly | more than one worker registered | Run only one `lanevoice-worker` at a time |
| Import errors running `python foo.py` | package not on path | Use `uv run …` (or `uv sync` first) |
| Practice: "needs a real model: set …" | the offline stub can't play a customer | Put `OPENROUTER_API_KEY` in `.env`; don't set `USE_LLM=false` |
| Practice: "Microphone unavailable" | permission denied, or a non-secure origin | Allow the mic; open `http://127.0.0.1:8710` (localhost is a secure origin) |
| Practice: "Scorecard unavailable — judge failed" | model/gateway hiccup at scoring time | The transcript is saved and the row records the error; run another session |
| Practice: "Report not emailed: SMTP not configured" | no `SMTP_HOST` / `SMTP_FROM` | Set both in `.env` (plus login if the relay wants one), restart the dashboard |
| Practice: no manager dropdown | the shipped roster is examples-only | Add `[[managers]]` entries to `src/lanevoice/practice/data/managers.toml`, restart |

Turn up logs for a call:
```bash
echo "LOG_LEVEL=DEBUG" >> .env
```

---

## 10. Command cheat-sheet

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
| Dashboard (runs, playground) | `make dashboard` | `uv run lanevoice-dashboard` |
| Practice a sales pitch | `make dashboard` | `uv run lanevoice-dashboard` → open `#/practice` |
| Build image | `make docker` | `docker build -t lanevoice:latest .` |
