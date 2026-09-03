# Warm transfer to the rep who owns the load — 8x8 plan

How the agent stops *saying* "I'm putting you through to Sarah" and actually puts
them through, to the person whose freight this is, on 8x8.

**Status (2026-09-02): Phase 1, Option A is implemented.** A handoff now resolves
the load's `CARRIERSALESREP` from Transport Pro's `internalContacts` (order taker
as fallback), reads that user's name and number from `GET /user/{id}` — `CELL`
first, then the `OFFICE` line; an extension is dropped and logged — lets the
desk override either with a `reps.toml` entry keyed on the Transport Pro user id
(a direct 8x8 DID belongs there), speaks "putting you through to <name>", and
then sends the SIP REFER via `transfer_sip_participant`. The audit log records
`requested` at the decision and `connected` or `failed: <reason>` after the
REFER; a failure is spoken to the caller ("a rep will call you straight back").
A rep with no diallable number is named but promised as a callback, never as a
transfer. The REFER itself is behind `SIP_TRANSFER_ENABLED` (off by default):
until it is on, the handoff is announced and logged as `not performed`.
**Prerequisite for turning it on:** call transfer and PSTN transfer enabled on
the Twilio trunk (§2A). Not yet done: the queue fallback (Option C) and the warm
shape (B′).

The original plan follows.

---

## 1. Where we are today

The call path is `carrier → Twilio Elastic SIP trunk → LiveKit SIP → call-* room
→ worker`. Twilio is the AI's carrier; LiveKit is the media brain; the flow logic
lives in `conversation/agent.py`.

Transfer is **decided but never performed**. Three concrete gaps:

| # | Gap | Where |
|---|-----|-------|
| 1 | No telephony action. `_transfer_and_say()` composes a sentence and ends the call state machine. Nothing dials anybody. | [agent.py:1422](../src/lanevoice/conversation/agent.py#L1422) |
| 2 | The audit log **lies**. `log_transfer(call_id, rep_id, "connected")` is written unconditionally — the DB says "connected" when no connection was attempted. | [agent.py:1513](../src/lanevoice/conversation/agent.py#L1513) |
| 3 | "The rep who owns this load" doesn't resolve on live data. The Transport Pro mapper hard-codes `assigned_rep_id=None`, so **every** live call falls through to `available_rep()` — whoever is first in a three-row seeded table of fake `+1555…` numbers. | [mappers.py:756](../src/lanevoice/integrations/transportpro/mappers.py#L756), [seed.py:81](../src/lanevoice/db/seed.py#L81) |

So the feature is really three features: **who** gets the call, **whether they can
take it**, and **how the audio actually moves**. Gap 3 is the one that decides
whether this feels like a real broker's desk or a call centre roulette wheel.

Thirteen call paths already route to `_transfer_and_say()` — `above_agent_authority`,
`fraud_review`, `ceiling_guard`, `booking_link_failed`, `source_unavailable`, … —
so every one of them starts working the moment the handoff is real. Nothing in the
negotiation logic needs to change.

---

## 2. How the audio moves — three mechanisms

### Option A — Cold transfer (SIP REFER) to the rep's 8x8 DID

LiveKit's `transfer_sip_participant` sends a SIP REFER up the Twilio trunk; Twilio
re-invites the carrier to the 8x8 number and drops LiveKit out of the call.

```python
await ctx.api.sip.transfer_sip_participant(
    api.TransferSIPParticipantRequest(
        participant_identity=sip_participant.identity,
        room_name=ctx.room.name,
        transfer_to="tel:+1XXXXXXXXXX",   # rep DID or queue DID on 8x8
        play_dialtone=False,
    )
)
```

- **Prerequisite:** call transfer **and** PSTN transfer must be enabled on the
  Twilio trunk (`--transfer-mode enable-all`). PSTN transfer is off by default
  precisely because it has billing consequences — see §6.
- Caller ID: Twilio's default presents the **transferee's** identity, i.e. the
  carrier's own ANI reaches 8x8. That is what we want — it makes an 8x8 screen pop
  possible (§5).
- **No custom SIP headers on a cold transfer** (open LiveKit request), so the load
  ID cannot ride along in-band. Context must go out-of-band (§5).
- Cheapest, ~40 lines of code, and 8x8 owns everything after the handoff:
  ring-no-answer, hunt, voicemail, its own recording and reporting.
- Cost: this is the *only* mechanism where LiveKit and the agent session stop
  billing at the moment of handoff.

### Option B — Warm/bridged transfer (dial the rep into the room)

Put the carrier on hold, `create_sip_participant` an outbound leg to the rep,
have the agent brief the rep in one sentence — *"Nick Patel at Patel Trucking,
MC 343195, load 2520571 Fort Wayne to Dallas, he's firm at $2,450, max buy is
$2,600"* — then unmute the carrier and drop out.

- Needs a LiveKit **outbound** trunk (`sip_setup/outbound-trunk.json`) — we only
  have an inbound one today.
- This is the version that earns its keep on `above_agent_authority`: the rep
  picks up already knowing the number, so they close instead of re-interviewing.
- We *know* whether a human answered, so gap 2 gets a truthful answer and a
  no-answer can roll to the next rep.
- Both legs sit in the LiveKit room for as long as the two humans talk, so LiveKit
  SIP minutes bill for the whole human conversation.

### Option B′ — Consult, then REFER (the recommended warm shape)

Bridge only for the **briefing**, then REFER the carrier to the rep and drop both
LiveKit legs. Full warm-transfer experience, bridged billing for ~45 seconds
instead of six minutes. It is the same code as B plus the REFER from A.

### Option C — Transfer to an 8x8 queue, not a person

REFER to a carrier-sales queue DID and let 8x8 route. Zero routing logic on our
side, free overflow/voicemail/callback, native 8x8 reporting — but it throws away
"the rep who owns this load", which is the whole ask.

Use C as the **safety net under A/B**, not as the primary path.

### Recommendation

**Phase 1: A + C** — REFER to the owning rep's DID, with the 8x8 queue DID as
fallback when no owner resolves, nobody is available, or the REFER fails.
**Phase 2: B′** for `above_agent_authority` and `fraud_review` only, where the
briefing changes the outcome. Everything else stays cold.

---

## 3. Who owns this load — the resolution chain

`TransferService.resolve()` becomes a pure function returning a `TransferPlan`
(destination, mode, reason, ordered fallbacks). No network calls, no LLM, fully
unit-testable — the same discipline as the negotiation engine.

Resolution order:

1. **Load owner from Transport Pro.** `/load/{id}` likely carries a salesperson /
   account-manager / created-by user. *Unverified* — `assigned_rep_id` was left
   `None` when the mapper was written. First thing to check:
   `lanevoice-tpcheck --load 2520571 --raw` and look for a user field.
2. **Terminal → POD owner.** This is the strong hook and it is already mapped:
   `load.terminal_id` resolves today, and on the live tenant the PODs are *named
   after the people who run them* — "POD (Carrigan Charnstrom)", "POD (Frankie
   Saiz)" ([terminals.py](../src/lanevoice/integrations/transportpro/terminals.py)).
   A terminal-id → rep mapping in the directory turns any load into a named human
   even if step 1 finds nothing.
3. **Office/team queue** for that terminal's office — an 8x8 queue DID.
4. **Any available rep** (today's behaviour), then voicemail-plus-callback-task.

Steps 1–2 are the feature. Steps 3–4 exist so a call never dead-ends.

### The rep directory

The `reps` table grows into the join between Transport Pro org structure and 8x8
identity:

```sql
ALTER TABLE reps ADD COLUMN extension    TEXT;  -- 8x8 extension
ALTER TABLE reps ADD COLUMN did          TEXT;  -- E.164, the REFER target
ALTER TABLE reps ADD COLUMN email        TEXT;  -- out-of-band brief
ALTER TABLE reps ADD COLUMN terminal_ids TEXT;  -- CSV of TP terminal ids owned
ALTER TABLE reps ADD COLUMN cc_agent_id  TEXT;  -- 8x8 Contact Center agent id
ALTER TABLE reps ADD COLUMN queue_did    TEXT;  -- fallback queue for this desk
```

Seeded from a CSV the ops team owns (`data/reps.csv`) so adding a rep is a data
change, not a deploy.

### Is the rep free?

Two worlds, and which one applies depends on the 8x8 tenant:

- **8x8 Contact Center** — the *Analytics for Contact Center Real-time Metrics
  API* (5-second refresh) and the *Managing Agent Status* API give real presence
  per agent and per queue. Poll on a 10–15 s cache; a stale read costs a wasted
  ring, so short TTL, and never block the call on it (2 s timeout, fail open to
  "try them anyway").
- **8x8 Work only (UCaaS, no CC)** — no presence API for plain extensions. Then
  availability is *empirical*: ring the DID with a 20 s timeout and treat
  no-answer as unavailable. Cold transfer can't observe that, which is an argument
  for B′ on the paths that matter.

`reps.available` stays as the manual override (a rep on leave), ANDed with live
presence.

---

## 4. Implementation plan

Deterministic decision in `services/`, network effect in `telephony/` — the LLM
still only writes the sentence.

### Phase 0 — Verify the assumptions (0.5–1 day)

| Check | How | Blocks |
|-------|-----|--------|
| Does the TP load payload name an owner? | `lanevoice-tpcheck --load <id> --raw` | step 1 of the chain |
| Terminal → rep mapping exists in real life? | `/terminal/search` dump vs. the org chart | step 2 |
| 8x8 tenant: Work only, or Contact Center? | 8x8 Admin Console / your CSM | presence + queue design |
| Does every rep have a direct DID, or extensions behind one main number? | Admin Console → numbers | REFER target format |
| Twilio REFER + PSTN transfer enabled? | Twilio Console → trunk → Call Transfer | everything |
| REFER actually works end to end? | one throwaway script: answer a call, REFER to a mobile | choice of A vs B |

Do the REFER smoke test before writing anything else. LiveKit's SIP transfer has
known provider-side failure modes (SIP 603 on some trunk configs); if REFER is
unreliable on this trunk, the plan shifts to B as primary and the estimate grows.

### Phase 1 — Routing (1–1.5 days)

- `domain/models.py`: extend `Rep`; add `TransferMode` (`COLD` / `CONSULT` / `QUEUE`),
  `TransferPlan(rep, destination, mode, reason, fallbacks)`, `TransferOutcome`.
- `db/database.py` + `db/seed.py`: directory columns, CSV loader, migration.
- `integrations/transportpro/mappers.py`: populate `assigned_rep_id` if Phase 0
  found the field.
- `services/transfer.py`: the resolution chain above → `TransferPlan`.
- `integrations/eightbyeight/` (new): `client.py` (presence, 2 s timeout, fail
  open), `directory.py` (terminal ⇄ rep ⇄ DID), `mappers.py`.
- Tests: owner found / owner busy / terminal-owned / queue fallback / nobody
  free / 8x8 API down.

### Phase 2 — Cold handoff + honest audit (1–1.5 days)

- `telephony/handoff.py` (new): executes a `TransferPlan` against the LiveKit API,
  returns `TransferOutcome`. Walks the fallback chain on failure.
- `conversation/agent.py`: `_transfer()` stashes `self.pending_transfer`; `_finish`
  logs `attempted`, not `connected`.
- `telephony/worker.py`: after speaking the handoff line, **await playout**, then
  execute. Firing the REFER while TTS is still streaming cuts the carrier off
  mid-sentence — this ordering is the single most common way this feature ships
  broken.
- `db/repository.py`: `log_transfer(call_id, rep_id, result, *, destination, mode,
  sip_status)`; results become `attempted` → `connected` / `no_answer` /
  `failed` / `queued`.
- Transport Pro note before the REFER, so the rep opens the load and sees what
  happened on the call (`add_load_note` already exists and is already used).
- Tests with a fake LiveKit API: REFER succeeds, REFER 603 → queue fallback,
  carrier hangs up mid-transfer, nobody available → callback task.

### Phase 3 — 8x8 polish (1 day)

Queue overflow rules, presence-aware skipping, optional screen pop: a tiny
read-only endpoint that 8x8 Contact Center can hit with the carrier's ANI to
return load ID / MC / last offer, so the rep's screen is already populated.

### Phase 4 — Warm consult-then-REFER (2–3 days)

Outbound trunk, hold + background audio for the carrier, one composed briefing
turn (the composer already knows the whole call), rep confirms, REFER, both LiveKit
legs drop. Enabled per-reason by config, starting with `above_agent_authority`.

### Phase 5 — Soak + docs (1 day)

Extend `docs/TEST_CALL_SCRIPTS.md` with transfer scenarios, add the runbook to
`docs/LIVE_SETUP.md`, dashboard the new `transfer_events` columns.

**Total: 7–10 engineering days**, Phases 0–2 (≈3–4 days) being the shippable
minimum that makes the promise true.

### New config

```env
TRANSFER_MODE=cold                  # cold | consult | queue | off
TRANSFER_WARM_REASONS=above_agent_authority,fraud_review
TRANSFER_RING_TIMEOUT_SECONDS=20
TRANSFER_FALLBACK_QUEUE_DID=+1XXXXXXXXXX
LIVEKIT_OUTBOUND_TRUNK_ID=ST_xxxx   # Phase 4 only
EIGHT_BY_EIGHT_BASE_URL=
EIGHT_BY_EIGHT_API_KEY=
EIGHT_BY_EIGHT_TENANT=
EIGHT_BY_EIGHT_PRESENCE_TTL_SECONDS=12
```

---

## 5. Context handoff without SIP headers

A cold REFER carries no metadata, so the rep must get context another way. In
descending order of value per unit of work:

1. **Transport Pro load note** — already implemented and already written on
   escalation. The rep's own system tells them the story. Free.
2. **Carrier ANI survives the transfer** (Twilio default), so the rep sees who is
   calling and an 8x8 screen pop can look the rest up.
3. **Email/SMS/Slack brief** at REFER time — one line with load, MC, last offer,
   max buy. Cheap, and it lands before they pick up.
4. **Spoken briefing** — Phase 4 only, worth it only where the number is in play.

---

## 6. Cost

All rates below were checked against the published pricing pages in August 2026;
treat them as a model to re-verify against your actual invoices, and note that
Twilio's REFER billing rules are the fiddliest part.

### Unit rates

| Component | Rate | Note |
|---|---|---|
| Twilio origination (PSTN → your trunk, US local) | **$0.0034 / min** | the inbound carrier leg |
| Twilio termination (your trunk → US PSTN) | **$0.0100 / min** | the leg to 8x8 |
| Twilio local number | $1.15 / mo | already paying |
| Initiating a REFER | **free** | you pay the referred destination's minutes |
| LiveKit third-party SIP minutes | **$0.004 / min** (Ship, after 5,000 incl.) · $0.003 (Scale) | per SIP leg in the room |
| LiveKit agent session minutes | **$0.010 / min** after allowance | while the AI session is live |
| LiveKit plan | Ship from **$50 / mo** | Build tier includes only 1,000 SIP min |
| 8x8 inbound to a DID/queue | **$0** marginal | included in the seat licence — confirm on your plan |

Twilio bills a REFER'd **origination** call transferred to **PSTN** as *mixed
origination + termination*: **$0.0134 / min** for the transferred portion. That
is the one number that drives this feature's bill, and it is worth confirming
with Twilio support before you enable PSTN transfer, because it is the reason the
setting is opt-in.

### Marginal cost per transferred call

Assuming a 6-minute human conversation after the handoff:

| Mechanism | Per-minute after handoff | 6-min transfer |
|---|---|---|
| **A — cold REFER** | $0.0134 (Twilio only) | **$0.080** |
| **B′ — consult 45 s, then REFER** | $0.0334 for 45 s, then $0.0134 | **$0.105** |
| **B — bridged the whole way** | $0.0234–$0.0334 | **$0.14–$0.20** |
| **C — REFER to queue** | same as A | **$0.080** |

Bridging adds $0.008/min (two LiveKit SIP legs) and another $0.010/min if the
agent session stays alive; Twilio charges the same either way, which is why B′
costs barely more than A while feeling like a real handoff.

### Monthly, at volume

1,000 inbound calls/month, 25% transferred (250), 6-minute rep conversations:

| Scenario | Monthly transfer cost |
|---|---|
| Cold (A + C fallback) | **≈ $20** |
| Consult-then-REFER on all transfers (B′) | **≈ $26** |
| Fully bridged (B) | **≈ $35–50** |

For scale: the AI portion of those same 1,000 calls (≈5 min each) already runs
about $0.017/min in telephony plus STT/LLM/TTS — roughly $85–90/month of trunk
and platform minutes before model costs. **The transfer feature adds ~20–25% on
top of existing telephony spend, i.e. tens of dollars a month, not hundreds.**

One threshold to watch: LiveKit's Ship plan includes 5,000 third-party SIP
minutes. 1,000 calls × 5 min sits exactly on that line, so bridged transfers are
what tip you into paid SIP minutes — another point for cold-by-default.

### Where the real cost is

**7–10 engineering days**, plus a few hours of Twilio and 8x8 configuration, plus
whatever an 8x8 CSM conversation costs if presence APIs or extra DIDs need
enabling. The per-minute spend is noise; the build and the org-chart data are the
cost. And the directory has an ongoing cost nobody budgets for: a rep joins,
leaves, or changes desks and the mapping goes stale — hence the CSV that ops
owns rather than a table only engineering can touch.

---

## 7. Risks and decisions needed

| Risk | Mitigation |
|---|---|
| REFER not supported/reliable on this Twilio trunk (SIP 603 class of failure) | Phase 0 smoke test; fall back to plan B as primary (+2–3 days) |
| Enabling PSTN transfer changes the bill | Confirm with Twilio before enabling; the $0.0134/min figure is the model |
| No load-owner field in Transport Pro | Terminal→POD mapping (step 2) already gets us a named human |
| 8x8 has no per-rep DIDs | Transfer to the main number + extension is not a REFER target — needs queue-with-agent-preference, or DIDs provisioned |
| Rep doesn't answer after a cold transfer | 8x8 ring-no-answer → queue → voicemail; we lose visibility, so log `queued` not `connected` |
| Recording/QA continuity breaks at the handoff | Accept the split (LiveKit up to transfer, 8x8 after) and cross-reference by call ID + ANI |
| 8x8 third-party SIP integration | Do **not** try to make 8x8 the AI's inbound carrier — 8x8 documents third-party SIP endpoints as best-effort with limited support, and X Series trunk changes require a support case. Keep Twilio as the carrier and 8x8 purely as a destination. |

**Needs a decision before Phase 1:**

1. Is the 8x8 tenant Contact Center, or Work only? (decides presence + queues)
2. Direct DID per rep, or extensions behind a main number?
3. Cold-everywhere, or warm on escalations? (my recommendation: cold now, warm on
   `above_agent_authority` in Phase 4)
4. Who owns the rep ⇄ terminal ⇄ 8x8 mapping day to day?

---

## Sources

- [LiveKit — cold transfer / call forwarding](https://docs.livekit.io/telephony/features/transfers/cold/)
- [LiveKit — warm transfer](https://docs.livekit.io/telephony/features/transfers/warm/)
- [LiveKit Cloud pricing](https://livekit.com/pricing)
- [Twilio — call transfer via SIP REFER](https://www.twilio.com/docs/sip-trunking/call-transfer)
- [Twilio — US SIP trunking pricing](https://www.twilio.com/en-us/sip-trunking/pricing/us)
- [8x8 — Analytics for Contact Center Real-time Metrics API](https://developer.8x8.com/analytics/reference/8-x-8-analytics-for-contact-center-real-time-metrics-api/)
- [8x8 — Contact Center APIs](https://support-portal.8x8.com/helpcenter/viewArticle.html?d=7237bf9a-1600-4eb5-8dc4-f3a5fa5533f3)
- [8x8 — X Series SIP trunk FAQ](https://support-portal.8x8.com/helpcenter/viewArticle.html?d=34c690b4-e252-471e-b7c9-2f16b6109639)
- [8x8 — third-party device support](https://support-portal.8x8.com/helpcenter/viewArticle.html?d=1f63580c-af23-4461-a943-54d3bf64df5b)
