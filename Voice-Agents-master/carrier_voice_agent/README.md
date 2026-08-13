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
make test             # 483 unit tests, no keys needed
make demo             # text simulation of every scenario
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
483 tests, offline. Includes the two real production load payloads and the
real Highway assessment shape.
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
| STT model | `STT_MODEL` | `openai/whisper-large-v3` |
| **LLM provider** | `LLM_PROVIDER` | `openrouter` (or `anthropic`) |
| LLM model | `LLM_MODEL` | unset = per provider: `anthropic/claude-haiku-4.5` (openrouter) or `claude-haiku-4-5` (anthropic) |
| LLM timeout (s) | `LLM_TIMEOUT` | `20.0` |
| TTS model | `TTS_MODEL` | `microsoft/mai-voice-2-flash` |
| TTS voice | `TTS_VOICE` | `en-US-Ethan:MAI-Voice-2` (must be a STABLE named voice) |
| TTS timeout (s) | `TTS_TIMEOUT` | `15.0` |
| Deadhead road factor | `DEADHEAD_ROAD_FACTOR` | `1.2` (driving / straight-line) |
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

## Out of scope (v1)
Outbound calling, multi-load calls, production fraud scoring beyond the
board-rate tripwire. FMCSA is not consulted directly — carrier vetting is
whatever Transport Pro reports for the MC/USDOT.
