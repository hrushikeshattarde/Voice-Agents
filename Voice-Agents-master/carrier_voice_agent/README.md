# LaneVoice — In-House Carrier-Sales Voice AI

An inbound carrier-sales voice agent: it answers a phone call, identifies the
load, verifies the carrier, negotiates the rate, and **books / warm-transfers /
declines** — with a full audit trail.

**Stack:** LiveKit (telephony + SIP) · **OpenRouter** for STT / LLM / TTS —
Whisper for transcription, **Claude Haiku 4.5** for every spoken turn,
**MAI-Voice-2** for the voice · **Transport Pro** Public API as the system of record ·
**Highway** for carrier qualifications and cargo insurance · typed, deterministic
Python business logic.

---

## Project layout

```
src/lanevoice/
├── settings.py            # all config (env-driven, typed) — one place to change models
├── logging_config.py
├── parsing.py             # extract load IDs / MC-DOT / money from utterances
├── geo.py                 # deadhead: spoken city -> point -> miles to the pickup
├── data/                  # bundled US city table (see data/SOURCE.md)
├── domain/                # typed models + enums (Load, Carrier, NegotiationResult, ...)
├── db/                    # Database (schema/seed) + Repository (typed data access)
├── services/              # the deterministic "product": loads, verification,
│                          #   negotiation engine, transfer
├── conversation/          # CarrierSalesAgent — the call state machine (the brain)
├── integrations/          # Transport Pro: client, mappers, repository, tpcheck,
│                          #   happyrobot (booking link + Highway invite)
│                          # Highway: client + mappers (qualifications, insurance)
├── datasource.py          # picks the backend from DATA_SOURCE
├── voice/                 # OpenRouterTTS + the composer (writes every spoken turn)
├── telephony/             # LiveKit worker (STT plugin + TTS adapter + lifecycle)
├── dashboard/             # web UI: runs + transcripts + negotiation ladders,
│                          #   analytics, browser playground (stdlib HTTP, no keys)
├── practice/              # sales-pitch trainer: customer personas (TOML data),
│                          #   voice loop, two judges (rubric + vocal delivery),
│                          #   call recordings, manager report email
└── demo.py                # text-mode simulation (no keys)
tests/                     # pytest: parsing, negotiation, verification, conversation,
                           #   Transport Pro client / mappers / repository / full calls
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
make test             # unit tests, no keys needed
make demo             # text simulation of every scenario
make dashboard        # web UI at http://127.0.0.1:8710 — runs, transcripts,
                      #   analytics, and a browser playground (no keys needed)
```

To take real calls, add credentials then run the worker:
```bash
cp .env.example .env  # fill in LiveKit + OpenRouter + Transport Pro
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
713 tests, offline. Includes the two real production load payloads, the real
Highway assessment shape, and the whole practice trainer (personas, judges,
speech legs, mailer — all against fakes that speak the real wire formats).
SIP wiring: [docs/LIVE_SETUP.md](docs/LIVE_SETUP.md) ·
call scripts: [docs/TEST_CALL_SCRIPTS.md](docs/TEST_CALL_SCRIPTS.md).

Console entry points (installed by `uv sync`):
`lanevoice-worker` · `lanevoice-demo` · `lanevoice-initdb` · `lanevoice-tpcheck`
· `lanevoice-dashboard`.

### Watching it work

`lanevoice-dashboard` serves the operations UI over the local audit database:
every run with its transcript and negotiation ladder, booking-rate analytics,
the board, and a **playground** that drives the real `CarrierSalesAgent` by
text in the browser — the same repository and negotiation engine the phone
worker uses, writing the same audit trail. Playground runs are labelled apart
from phone runs. Stdlib HTTP only (no new dependencies), binds to 127.0.0.1,
and never sends a secret to the browser. It does not answer the phone —
`lanevoice-worker` does — and both run happily side by side.

The **Practice** tab is the same machinery pointed at training the humans —
see [Practice mode](#practice-mode) below.

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
| **Office scope** | `TRANSPORT_PRO_OFFICE_TERMINAL_CODE` | empty = whole company board; `1001` = Fort Wayne |
| Pin terminal ids instead of walking | `TRANSPORT_PRO_OFFICE_TERMINAL_IDS` | — |
| Unattributable load in scope? | `TRANSPORT_PRO_ALLOW_UNKNOWN_TERMINAL` | `false` (exclude) |
| API timeout (s) | `TRANSPORT_PRO_TIMEOUT` | `10.0` |
| Fraud tripwire, share of board rate | `TRANSPORT_PRO_FRAUD_LOW_RATIO` | `0.5` |
| Load numbers read aloud on a miss | `TRANSPORT_PRO_MAX_OFFERED_LOADS` | `5` |
| Search page cap (runaway backstop) | `TRANSPORT_PRO_MAX_SEARCH_PAGES` | `10` (= 2000 loads) |
| Highway token *(optional)* | `HIGHWAY_API_TOKEN` | — (checks skipped without it) |
| Highway timeout (s) | `HIGHWAY_TIMEOUT` | `8.0` |
| Prefer Highway's company name | `HIGHWAY_PREFER_COMPANY_NAME` | `true` |
| Booking-link endpoint *(optional)* | `HAPPYROBOT_URL` / `HAPPYROBOT_TOKEN` | — (offers log without a link) |
| STT model | `STT_MODEL` | `openai/whisper-large-v3-turbo` (chosen on accuracy for spoken rates — see settings.py) |
| **LLM provider** | `LLM_PROVIDER` | `openrouter` (or `anthropic`) |
| LLM model | `LLM_MODEL` | unset = per provider: `anthropic/claude-haiku-4.5` (openrouter) or `claude-haiku-4-5` (anthropic) |
| LLM timeout (s) | `LLM_TIMEOUT` | `20.0` |
| TTS model | `TTS_MODEL` | `microsoft/mai-voice-2-flash` |
| TTS voice | `TTS_VOICE` | `en-US-Ethan:MAI-Voice-2` (must be a STABLE named voice) |
| TTS timeout (s) | `TTS_TIMEOUT` | `15.0` |
| Deadhead road factor | `DEADHEAD_ROAD_FACTOR` | `1.2` (driving / straight-line) |
| Phrase via LLM? | `USE_LLM` | `false` (fast templates) |
| Turn buffer (s) | `MIN_ENDPOINTING_DELAY` | `1.3` |
| VAD: speech confidence to count as a turn | `VAD_ACTIVATION_THRESHOLD` | `0.55` |
| VAD: minimum speech length (s) | `VAD_MIN_SPEECH_DURATION` | `0.10` |
| Barge-in: speech needed to cut the agent off (s) | `MIN_INTERRUPTION_DURATION` | `0.9` |
| Resume a line cut by noise (no transcript)? | `RESUME_FALSE_INTERRUPTION` | `true` |
| Speak a filler when a reply takes longer (s, 0=off) | `FILLER_DELAY` | `0.8` |
| Negotiation rounds | `MAX_NEGOTIATION_ROUNDS` | `8` |
| Reserve below Max Buy | `NEGOTIATION_BUFFER` | `0` (may reach Max Buy) |
| Share of their move we return | `NEGOTIATION_RECIPROCITY` | `0.5` (lower = firmer) |
| Agent's own authority | `NEGOTIATION_DISCRETION_RATE` | `0.6` of floor→Max Buy |
| Gap not worth haggling | `NEGOTIATION_SETTLE_GAP_RATE` | `0.10` |
| Gap that triggers the split close | `NEGOTIATION_SPLIT_GAP_RATE` | `0.30` |
| Best-and-final if they never moved | `NEGOTIATION_STONEWALL_FINAL_RATE` | `0.5` |
| Pushes before best-and-final | `NEGOTIATION_MAX_HOLDS` | `2` |
| **Record phone calls?** | `RECORD_CALLS` | `false` — see the consent + retention notes in settings.py before enabling. On: each call saves to `call_recordings/<call_id>.ogg` and plays in the Runs drawer |
| **Practice:** rep turns per session | `PRACTICE_MAX_TURNS` | `40` (also the cost ceiling on a forgotten tab) |
| Practice: customer line budget | `PRACTICE_REPLY_MAX_TOKENS` | `220` |
| Practice: judge budget / timeout | `PRACTICE_JUDGE_MAX_TOKENS` / `PRACTICE_JUDGE_TIMEOUT` | `4000` / `60.0` — 2000 truncated a real verdict, twice |
| Practice: vocal-delivery judge | `PRACTICE_DELIVERY_MODEL` | `google/gemini-3.7-flash` (probe-chosen; empty = vocal judging off) |
| Practice: keep raw voice clips? | `PRACTICE_KEEP_AUDIO` | `false` — turn clips die at scoring; the stitched call recording is kept |
| Practice report email | `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM` | — / `587` / — (unset = reports stored, never mailed) |
| SMTP login (if the relay wants one) | `SMTP_USERNAME` / `SMTP_PASSWORD` | — |
| STARTTLS on the SMTP session | `SMTP_STARTTLS` | `true` |

Change a model = change one line (or one env var). Nothing else hard-codes it.

## API keys

| Service | Vars | Purpose |
|---|---|---|
| **LiveKit** ([cloud.livekit.io](https://cloud.livekit.io)) | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | telephony + SIP + phone number |
| **OpenRouter** ([openrouter.ai/keys](https://openrouter.ai/keys)) | `OPENROUTER_API_KEY` | STT + LLM + TTS — all three AI hops |
| **Transport Pro** Public API | `TRANSPORT_PRO_URL`, `TRANSPORT_PRO_USERNAME`, `TRANSPORT_PRO_PASSWORD` | loads, carrier vetting, contacts, offers |
| **Highway** (optional) | `HIGHWAY_API_TOKEN` | carrier qualifications (authoritative), cargo insurance limits, trading name. A JWT with a hard expiry |
| **Transport Pro HappyRobot** (optional) | `HAPPYROBOT_URL`, `HAPPYROBOT_TOKEN` | the carrier booking link, and the Highway connect invite |
| **Anthropic** (optional) | `ANTHROPIC_API_KEY` | the same Claude Haiku 4.5 first-party, one hop fewer. Needed *in addition to* the OpenRouter key, since speech still runs there |

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

**Booking is a link, not a write.** With `HAPPYROBOT_*` configured, agreeing a
rate runs `POST /offer` then `accept_offer` and the carrier is sent a real
`book_now_url`. The agent then says *"open it and sign to lock it in"* and is
explicitly forbidden from saying **"booked"** — the load stays on the board until
they complete it, so a slow carrier genuinely does lose it. Without those
credentials the rate is logged as an offer for a rep instead, and the worker warns
about that at startup.

If the link can't be issued the call goes to a rep rather than falling back to
logging a second offer, and the call note says whether the rate landed — a rep who
guesses wrong either double-sells the lane or never places it. `summary()` carries
`booking_link_sent` so a booking awaiting a signature is countable apart from one
awaiting a rep.

## Office scope

A deployment can be restricted to one office's freight by setting
`TRANSPORT_PRO_OFFICE_TERMINAL_CODE`. Scope is the office terminal **plus every
POD and team parented under it**, walked from `GET /terminal/search` and cached
for an hour.

The subtree is not optional. Measured on the live tenant, Fort Wayne Office
(`terminalCode "1001"`, id 1003) carries **4** posted loads while its 49 PODs
carry another **338** — POD (Carrigan Charnstrom) alone has 80. Scoping to the
office id would hide 99% of the office's own freight while appearing to work.

Both load paths are gated: a number the caller reads out (`get_load`) and the
alternatives read back when it misses (`open_loads`). `terminalId` is a real
server-side filter on `/load/search` but matches **one** terminal exactly — it
does not descend the tree, and a comma-separated list is ignored — so the board
scan makes one request per terminal and stops at the cap. It is also re-checked
per record, because a filter the endpoint silently ignored would put another
office's freight in the agent's mouth.

Both search endpoints are fully paged (`page` is the only parameter this API
honours; `perPage` is ignored). The board scan is a generator, so it reads page 0,
finds its five loads and stops — one request. Reading the whole board takes four.

A load whose terminal can't be read is treated as **out** of scope, and an office
code that resolves to nothing falls back to no filtering with a loud log — an
empty scope read as "nothing is sellable" would be an agent that can't sell at all.

## Deadhead

The agent asks where and when the truck frees up, then tells the carrier roughly
how far that is from the pickup — *"it's about ninety miles from you"*. Pickup
coordinates come off the load's own waypoint; the caller's spoken city is resolved
against a bundled 3,407-place US table
([`data/SOURCE.md`](src/lanevoice/data/SOURCE.md)), so there is **no geocoding
call on the critical path and no extra key**.

It is a straight-line distance scaled by `DEADHEAD_ROAD_FACTOR` — measured 3–15%
over real driving miles on five known routes, mean 8.8%, and over is the safe
direction. It is therefore **always spoken rounded** ("about 90 miles", never "97
miles") and **never feeds a rate**; pricing off it would need real road miles from
a routing engine.

When the caller can't be placed confidently — a state with no city, a town below
the table's floor, a name mangled past a tight fuzzy match — the agent says
nothing about distance at all. A confidently wrong deadhead is worse than none,
because a driver plans around it.

## Carrier qualification

Roughly one posted load in ten demands a carrier classification (Critical Cargo,
Temperature Controlled, …), and Transport Pro's `carrier_status` returns **no
classification list at all**. So a carrier is vetted *for the specific load*:
Highway's `rules_assessment` decides where it has an opinion — authoritative in
both directions, because Transport Pro's list has been observed wrong each way —
and falls back to Transport Pro's own list otherwise. An unmet requirement is a
warm transfer, never a decline: it is a fact about the freight, not a judgement on
the carrier. Without a Highway token the checks are skipped, loudly.

## Practice mode

The dashboard's **Practice** tab flips the table: the model plays a CUSTOMER in
one of eight moods a freight desk actually meets — the Brush-off, the Rate
Shopper, the Burned Shipper, the Busy Operator, the Chatty Non-committer, the
Skeptical Negotiator, the Gatekeeper, the Loyal Incumbent — and the human rep
makes the pitch, by voice (hold-to-talk in the browser) or by text.

Personas are **data, not code**: one TOML each under
[practice/data/profiles/](src/lanevoice/practice/data/profiles/), carrying the
mood, the speech style, the hidden facts a good discovery question earns, what
warms the customer up and what makes them hang up, and the session's win
condition. A malformed file stops the dashboard at boot with the field named.
The browser gets the picker card only — the hidden material never leaves the
server, so a rep can't read the answer key with devtools.

Every finished session is **scored twice and measured once**:

* **Conversation** — a fixed eight-dimension rubric (opening, discovery,
  listening, objection handling, value, composure, closing, plus a per-persona
  focus), judged by the composer model with a verbatim quote behind every score
  and a concrete better line behind every improvement, and a strict verdict on
  whether the rep actually achieved the goal.
* **Vocal delivery** (voice sessions) — confidence, clarity, energy, pace and
  warmth, judged by an audio-input model that hears the rep's actual clips
  (`PRACTICE_DELIVERY_MODEL`; no Claude model accepts audio, so this is the one
  place practice reaches past the composer's provider).
* **Metrics** — deterministic arithmetic no model can fudge: talk ratio,
  questions asked, fillers per minute, words per minute, pause ratio, leading
  hesitation. Week-over-week progress you can trust.

Both sides of a voice call are stitched into a **replayable recording** served
in the report view; the raw per-turn clips are deleted the moment scoring
finishes unless `PRACTICE_KEEP_AUDIO=true` — they are recordings of your reps,
so keeping them is a decision, not a default. Pick an **account manager** at
session start (roster:
[practice/data/managers.toml](src/lanevoice/practice/data/managers.toml)) and
the scored report is emailed to them — stdlib SMTP, gated on
`SMTP_HOST`/`SMTP_FROM`, and non-fatal by design: a dead mail server is
recorded on the report row, never a lost scorecard. Judge failures degrade the
same way. The transcript is always safe first.

Practice needs a real model (`OPENROUTER_API_KEY`); it refuses the offline stub
by naming the setting, because a customer with no model can't hold a
conversation worth practicing against. Sessions cost roughly a cent each —
persona turns, two judge calls, and speech for voice mode.

**Scripts to exercise every persona and every path:**
[docs/PRACTICE_SCRIPTS.md](docs/PRACTICE_SCRIPTS.md) — worked strong/weak plays
per customer mood, plus the system checks (recordings, retention, email states).

## Out of scope (v1)
Outbound calling, multi-load calls, production fraud scoring beyond the
board-rate tripwire. FMCSA is not consulted directly — carrier vetting is
whatever Transport Pro reports for the MC/USDOT.
