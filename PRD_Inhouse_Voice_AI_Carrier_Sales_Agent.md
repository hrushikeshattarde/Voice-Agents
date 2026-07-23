# PRD: In-House Voice AI Agent for Inbound Carrier Sales Calls
**Codename:** LaneVoice (placeholder — rename freely)
**Owner:** [you]
**Status:** Draft v1
**Last updated:** July 2026

---

## 1. Overview

Build an in-house voice AI agent that answers inbound calls from carriers asking about posted loads, verifies the carrier (MC/DOT + Load ID), negotiates rate against a floor pulled live from Transport Pro (your TMS), and — when the carrier won't move to an acceptable rate — performs a warm transfer to the carrier sales rep assigned to that load in Transport Pro.

This is functionally a self-built version of what HappyRobot/Vooma sell as a managed product. This PRD assumes that decision has been made and scopes the build.

---

## 2. Goals & Non-Goals

**Goals**
- Answer 100% of inbound carrier-sales calls with no busy signal / voicemail during business hours
- Automate the verify → negotiate → book-or-transfer flow for the majority of calls without a human on the line
- Never book a load below the TMS-defined floor price, under any conversational path (including adversarial ones — see §9.4)
- Transfer to the correct human rep, for the correct load, with full context, when negotiation fails
- Full audit trail: transcript, offer history, and outcome logged against the Load ID in your systems

**Non-goals (v1)**
- Outbound calling / proactive carrier sourcing (that's a distinct project — revisit post-launch)
- Multi-load batching in a single call (v1 assumes one load per call)
- Appointment scheduling, check calls, or POD collection (separate workflows, can reuse the same voice stack later)
- Non-English calls (add language support post-MVP if warranted by call volume)

---

## 3. Call Flow — Functional Spec

```
1. INBOUND CALL RECEIVED
   → Greet, ask which load the carrier is calling about (Load ID or lane)

2. IDENTIFY LOAD
   → Query Transport Pro for Load ID → confirm origin/destination/pickup date back to carrier
   → If Load ID not found / already covered → inform caller, offer to check other open loads, end gracefully

3. VERIFY CARRIER
   → Capture MC or USDOT number (voice + confirm by repeating back)
   → Verify authority status (active, not revoked) via carrier verification service (§9.2)
   → Flag risk signals: recently reactivated authority, insurance lapse, name mismatch (see §9.2)
   → If verification fails or high-risk flag → do NOT proceed to rate discussion; route to a human review queue (do not just hang up — fraud attempts should be logged, not silently dropped)

4. STATE PRICE / CAPTURE COUNTER-OFFER
   → State the posted rate for the load
   → Listen for carrier's counter-offer (accept, counter, or decline)

5. NEGOTIATE
   → Compare counter-offer against TMS floor/ceiling for that Load ID
   → If counter ≥ floor → counter-negotiate toward target, or accept if at/above target
   → Loop up to N rounds (configurable, default 3) or until convergence
   → All accept/reject decisions are evaluated by a deterministic server-side check against
     the live TMS floor — never by LLM judgment alone (see §9.4)

6. RESOLVE
   6a. AGREEMENT REACHED → confirm rate verbally, write booking back to Transport Pro,
       trigger rate confirmation doc, end call
   6b. NO AGREEMENT, CARRIER WANTS TO TALK TO A HUMAN → look up the carrier sales rep
       assigned to this Load ID in Transport Pro → warm-transfer with a whisper/context
       summary (load, carrier, MC#, offers made) → if rep unavailable, fall back per §9.5
   6c. NO AGREEMENT, CARRIER DISENGAGES → log outcome, end call politely, load stays open

7. POST-CALL
   → Full transcript + structured outcome (JSON) written to your data store and linked to
     Load ID; summary event pushed to Transport Pro / Slack per your ops preference
```

---

## 4. System Architecture

```
                                   ┌─────────────────────────┐
   PSTN / Carrier phone  ───────▶  │  Telephony / SIP layer   │  (Twilio or Telnyx)
                                   └────────────┬─────────────┘
                                                │ media stream (WebSocket)
                                                ▼
                                   ┌─────────────────────────┐
                                   │  Voice Orchestration      │  (Pipecat or LiveKit Agents,
                                   │  Framework (self-hosted)  │   containerized)
                                   └───┬─────────┬─────────┬──┘
                                       │         │         │
                          ┌────────────┘         │         └────────────┐
                          ▼                       ▼                      ▼
                 ┌────────────────┐    ┌────────────────────┐   ┌────────────────┐
                 │  STT (streaming)│    │  LLM(s) — tiered    │   │  TTS (streaming) │
                 │  Deepgram Nova-3│    │  routing (§7)       │   │  Cartesia Sonic  │
                 │  / Flux         │    └─────────┬───────────┘   │  or ElevenLabs   │
                 └────────────────┘              │                └────────────────┘
                                                   │ tool/function calls
                                                   ▼
                          ┌──────────────────────────────────────────────┐
                          │            Business Logic / Orchestrator API  │
                          │  (your own service — the actual "product")    │
                          │                                                │
                          │  • Load lookup           • Negotiation engine │
                          │  • Carrier verification   • Floor/ceiling      │
                          │    (FMCSA + fallback)       enforcement (hard  │
                          │  • Transport Pro read/write  server-side check)│
                          │  • Rep lookup + transfer   • Call state machine│
                          └───────────────┬────────────────────────────────┘
                                           │
                     ┌─────────────────────┼─────────────────────┐
                     ▼                     ▼                     ▼
            ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────┐
            │  Transport Pro    │  │  Carrier verify    │  │  Postgres + Redis   │
            │  (API/EDI/webhook)│  │  API (FMCSA +      │  │  (loads, carriers,  │
            │                   │  │  fallback service)  │  │  calls, offers,     │
            └─────────────────┘  └──────────────────┘  │  transcripts)        │
                                                          └────────────────────┘
```

**Design principle:** the LLM is the *conversational interface only*. It never directly commits a booking, never directly decides "this MC number is valid," and never has authority to accept a price. Every consequential decision goes through a deterministic function call into your Business Logic layer, which is plain code you can unit-test. This matters both for correctness (LLMs make mistakes) and for security (a caller cannot talk the model into a bad outcome if the model has no power to cause one — see §9.4).

---

## 5. Tech Stack Recommendation

### 5.1 Telephony / SIP

| Option | Recommendation | Why |
|---|---|---|
| **Twilio** | **Primary recommendation for MVP** | Most mature docs/SDKs, best-documented `<Dial>`-based warm transfer, broadest carrier compliance posture (useful if you later expand internationally). Premium per-minute rate is a small line item at your volume. |
| **Telnyx** | Cost-optimization path once stable | Runs its own private backbone (measured ~40–90ms lower latency vs. Twilio in third-party tests); roughly 30–70% lower carrier cost at scale; native Conversation Relay product bundles STT in-network. Migration is close to drop-in (TeXML is TwiML-compatible). |
| Hack worth knowing | — | You can route a Telnyx trunk through Twilio's BYOC to keep Twilio's developer experience while paying Telnyx rates. |

**Recommendation:** start on Twilio for reliability and documentation while you're building the hard part (the negotiation logic); revisit Telnyx once call volume is stable and the cost delta is worth the migration effort.

### 5.2 Voice Orchestration Framework (the STT↔LLM↔TTS pipeline glue)

| Framework | License | Best for | Trade-off |
|---|---|---|---|
| **Pipecat** | BSD-2, ~13.4k GitHub stars, v1.1 (Apr 2026) | **Recommended starting point.** Python, pipeline-of-frame-processors model, 60+ service integrations, built-in debugging (Whisker, Tail) and OpenTelemetry support | You own more of the orchestration logic than a managed platform gives you — this is the point, since you're building the product, not renting it |
| **LiveKit Agents** | Apache 2.0, ~11.4k stars | Best choice if/when you need to scale past roughly 100+ simultaneous calls or want native SIP without a separate telephony adapter — WebRTC "room" model scales differently than a linear pipeline | Steeper infra setup; you're now also running/operating a WebRTC SFU |
| TEN Framework | Open source | Multimodal (voice+vision) orchestration | Overkill for a pure phone-call use case |
| Bolna | Open source | Fastest path to "phone agent live this afternoon," strong at outbound dialing | Smaller community, less proven at your negotiation-logic complexity |

**Recommendation:** build the MVP on **Pipecat**. It's the framework most teams doing exactly this kind of custom client work reach for by default in 2026, and it won't lock you out of moving to LiveKit Agents later if call concurrency demands it — the STT/LLM/TTS provider integrations are largely portable concepts either way.

### 5.3 Speech-to-Text (STT)

| Provider | Latency | Notes |
|---|---|---|
| **Deepgram Nova-3 (+ Flux)** | Sub-300ms streaming | **Recommended.** Flux is purpose-built with model-integrated end-of-turn detection — directly useful for a negotiation call where you need to know precisely when the carrier has finished making an offer vs. just paused. SOC 2/HIPAA, on-prem option available. |
| AssemblyAI Universal-2/3 Pro | Comparable latency | Best documented price-performance; bundles entity extraction/sentiment which could double as a secondary signal in negotiation calls |
| Whisper (open source, self-hosted) | Higher latency unless heavily optimized | Only worth it if you're already running GPU infra for your LLM and want to consolidate; not the default choice for a latency-sensitive negotiation call |

### 5.4 Text-to-Speech (TTS)

| Provider | Time-to-first-audio | Notes |
|---|---|---|
| **Cartesia Sonic (Turbo)** | ~40ms | **Recommended for raw latency** — SSM architecture gives consistent latency including at P99, which matters more than average latency for a live negotiation where dead air reads as hesitation/weakness to a carrier |
| ElevenLabs Flash v2.5 | ~75ms | Best voice library/expressiveness if brand voice matters; per-minute pricing gated to Business tier |
| Deepgram Aura-2 | ~90–250ms | Best pick if you need on-prem/compliance-heavy deployment (pairs natively with Deepgram STT on the same runtime, reducing pipeline hops) |

### 5.5 LLM — the core decision

This is the part your question specifically asked about, so it gets its own section: **§7**.

### 5.6 Data Layer

| Component | Recommendation | Why |
|---|---|---|
| Primary DB | **Postgres** (RDS, Neon, or Supabase) | Structured records: loads (mirrored/cached from Transport Pro), carriers, calls, negotiation offers, transfer events. Use Postgres, not a NoSQL store — this data is relational and you'll want to run real SQL reports on negotiation outcomes |
| Session/real-time state | **Redis** | In-call conversation state, active negotiation round tracking, rate limiting |
| Call recordings/transcripts | **S3 (or equivalent) + encryption at rest** | Retention policy driven by your compliance requirements (see §9.3) |
| Analytics | Start with SQL on Postgres; add a warehouse (BigQuery/Snowflake) only once volume justifies it | Don't over-build analytics infra before you have negotiation data to analyze |

### 5.7 Hosting / Infrastructure

| Layer | Recommendation | Why |
|---|---|---|
| Voice orchestration compute | Containerized, deployed on **ECS Fargate or Cloud Run for MVP**, migrate to **Kubernetes (EKS/GKE)** only once you need finer-grained autoscaling for concurrent-call spikes | Don't stand up Kubernetes on day one for a system handling a few thousand calls/month — it's operational overhead you don't need yet |
| Region | Deploy compute in the **same region as your telephony provider's nearest edge** (us-east-1 for most US carrier traffic on Twilio/Telnyx) | Every hop adds latency; region mismatch is a common, avoidable cause of "the agent feels slow" |
| GPU compute (only if self-hosting an open-weight LLM — §7.3) | Serverless GPU endpoints (**Fireworks, Baseten, Together**) over raw GPU rental (RunPod/Lambda) for a small team | Managed endpoints remove cold-start and ops burden; raw GPU rental only pays off once you have dedicated MLOps capacity |
| IaC | Terraform | Standard, keeps infra reviewable/auditable — relevant given your security background |
| CI/CD | GitHub Actions (or your existing pipeline) | No special requirement here beyond standard practice |

### 5.8 Observability & QA

- **OpenTelemetry tracing** end-to-end (Pipecat has native support) — trace every call by call ID through STT → LLM → business logic → TTS
- **Call-level dashboards**: latency (STT/LLM/TTS/round-trip), negotiation outcome distribution, transfer success rate, MC-verification failure rate
- **Voice agent eval/simulation tooling**: run a regression suite of simulated carrier calls (happy path, low-ball offers, hostile/adversarial callers, ambiguous Load IDs, mumbled MC numbers) before every deploy — this is the equivalent of unit tests for a conversational system and is the single most under-budgeted piece of a voice AI build. Purpose-built tools exist for this (e.g., Coval, Cekura) if you don't want to build the harness yourself.
- **Alerting**: failed transfers, FMCSA/verification-service outages, negotiation floor violations (should be structurally impossible per §9.4, but alert on it anyway — defense in depth)

---

## 6. Transport Pro Integration — Read This Before Scoping a Timeline

This is the highest-risk dependency in the whole project, and it's worth flagging plainly: **Transport Pro's public materials emphasize an in-house-managed EDI model** ("our EDI technical team handles all of your EDI in-house... our devs will work directly with your trading partners") **rather than a self-serve developer API/webhook portal** of the kind HappyRobot's connector implies exists. That doesn't mean an API doesn't exist for partners — HappyRobot clearly has one — but it does mean you should not assume open, documented, self-service access.

**Action item #1, before writing any other code:** contact Transport Pro directly and get, in writing:
- Whether a REST/webhook API exists for third-party read (load status, pricing) and write (booking, status updates) access, or whether integration happens through their in-house EDI team on a per-partner basis
- What authentication model it uses (API key, OAuth)
- Rate limits and SLA
- Whether "load floor/ceiling price" and "assigned carrier sales rep" are fields your integration can actually read per-load (both are load-bearing requirements for steps 5 and 6b of your call flow)
- Sandbox/test environment availability

If Transport Pro's integration path turns out to be EDI-only (X12 204/990/210/214 transaction sets) rather than a real-time API, that changes your architecture meaningfully — EDI is batch/document-oriented and not well-suited to a live "check the floor price mid-call" lookup. In that case, plan on Transport Pro (or a middleware layer like a dedicated integration partner) maintaining a near-real-time mirror of load data in your own Postgres instance instead of querying Transport Pro live on every call.

---

## 7. LLM Selection

### 7.1 The core trade-off

You asked specifically about open-source vs. OpenRouter. Here's the honest framing: for a negotiation call where a wrong or manipulable decision costs you real margin, **model choice matters less than the deterministic guardrails around the model** (§9.4) — but within that constraint, here's how to think about it.

| Approach | What it means | When it's right for you |
|---|---|---|
| **Direct frontier API** (Anthropic Claude, OpenAI GPT) | Call the provider's API directly | Default choice for the negotiation turn itself — see §7.2 |
| **OpenRouter** | One API key, 315+ models (frontier + open-weight) behind a unified OpenAI-compatible endpoint, pay-per-token, ~5.5% platform fee | Best during development/pilot: lets you A/B test GPT-4.1 vs. Claude Sonnet 4.6 vs. an open-weight model on real call transcripts without re-integrating each one. Adds 35–150ms routing overhead at P95 vs. a direct call — acceptable during evaluation, worth removing once you've picked a winner and have sustained volume. |
| **Self-hosted open-weight** (Llama 3.3 70B, Qwen3, Nemotron 3 Super) via vLLM | You run the model on your own (or rented) GPUs | Only clears its own cost bar at meaningful token volume — see §7.3 for the actual breakeven math. Also the right call if your financial-services/security background surfaces a hard data-residency requirement (call audio/transcripts never leaving infra you control) that a vendor API can't satisfy contractually. |

### 7.2 Recommended model tiering for this specific call flow

Not every turn in the call needs the same model. Use a two-tier approach:

**Tier 1 — Negotiation & verification-decision turns** (the turns that actually matter: interpreting a counter-offer, deciding whether to escalate, handling an ambiguous or adversarial statement)
- **Claude Sonnet 4.6** or **GPT-4.1**, called directly (not through a router, once you're past pilot).
- Both are legitimate defaults; the honest difference from current benchmarking: GPT-4.1 is the most widely deployed default in production voice agents today (cited as the model behind the majority of a leading voice platform's call volume) on latency/cost/function-calling reliability grounds; Claude Sonnet 4.6 has a documented edge in **holding fidelity to a long, detailed system prompt across many conversational turns** and in **tail latency consistency** (its P95/P50 latency ratio was measured as the best among frontier providers in mid-2026 testing) — both matter for a negotiation script with a lot of "never do X" business rules embedded in the prompt.
- **Do not enable extended "reasoning" mode on the live turn** — reasoning-mode time-to-first-token is measured in seconds (not milliseconds) on current frontier models, which is unusable inside a real-time call. Reserve reasoning-mode calls for offline/post-call steps (e.g., generating a call summary, flagging a transcript for QA review).

**Tier 2 — Simple slot-filling turns** (capturing/confirming a Load ID, repeating back an MC number, routing intent classification)
- A smaller, cheaper, faster model: **GPT-4.1 mini/nano**, or an **open-weight model via OpenRouter** (Llama 3.3 70B and NVIDIA Nemotron 3 Super are both available on OpenRouter's free tier for light volume, or at a few cents per million tokens at production volume).
- These turns are low-complexity extraction tasks; a frontier model is overkill and adds unnecessary latency/cost for no quality gain here.

### 7.3 Self-hosting math (open-weight models)

If you later want to self-host instead of calling an API: published 2026 benchmarks put the break-even point at **roughly 5–10 million tokens/month** vs. a premium API (GPT-4.1/Claude-class) and **50–100 million tokens/month** vs. a budget API (GPT-4.1 mini-class). Below that, the fixed cost of GPU infrastructure and the engineering time to run vLLM in production (autoscaling, cold starts, model updates) outweighs the token savings. At your likely call volume, **you will not clear this breakeven in year one** — treat self-hosting as a cost-optimization to revisit once you have 6+ months of real production token counts, not a day-one architecture decision.

### 7.4 Practical recommendation

1. **Pilot phase:** build on OpenRouter so you can cheaply run the same call scripts against Claude Sonnet 4.6, GPT-4.1, and one open-weight model, and score them against your own eval harness (§5.8) — not vendor benchmarks.
2. **Production:** move the winning Tier-1 model to a **direct API integration** (drop OpenRouter's fee and routing latency once you're not actively comparison-shopping), keep Tier-2 on whichever cheap model tested best, optionally still through OpenRouter since the latency budget matters less on slot-filling turns.
3. **Revisit self-hosting** only once you have real monthly token volume data and/or a data-residency requirement that a hosted API genuinely can't meet.

---

## 8. MC/DOT Carrier Verification

### 8.1 Primary data source

The federal source of truth is FMCSA's **SAFER** system, queryable via the **QCMobile API** (free, requires a Login.gov developer account and API "WebKey"; returns carrier authority, safety, and insurance data by USDOT number, MC number, or name).

**Important current-state flag:** as of this writing, FMCSA's own Mobile Developer site and QCMobile/SaferBus web services have shown extended outages ("currently down... no established timeframe" per their own status notice). **Do not architect a single point of failure on the free government API.** This is good practice regardless of current uptime, but it's an active, not hypothetical, risk right now.

### 8.2 Recommended verification architecture

- **Primary:** FMCSA QCMobile API (free, authoritative, but not contractually reliable)
- **Fallback / cross-check:** a commercial carrier-verification API or data reseller (several exist that mirror/cache FMCSA data with an SLA — evaluate options in the same category as **Highway**, **RMIS**, **MyCarrierPortal**, or **DAT CarrierWatch**) so a FMCSA outage doesn't take down your entire call-answering capability
- **Fraud-pattern layer, beyond basic authority lookup:** a "valid MC number" check is necessary but not sufficient — flag on the signals that basic SAFER data won't surface on its own: authority reactivated in the last 30–90 days, MCS-150 filing date very recent relative to an otherwise-dormant-looking record, name/company mismatch between what the caller states and what's on file. Build this as an explicit rule layer on top of the raw lookup, not something you expect the LLM to infer from context.

### 8.3 A regulatory note that affects your data model

FMCSA is transitioning identification away from the standalone **MC number** toward the **USDOT number** as the single identifier under its Unified Registration System (URS), specifically to close fraud loopholes tied to MC-number churn/reactivation. Your carrier data model and verification prompt should treat **USDOT number as primary** and MC number as secondary/legacy, not the other way around, so you're not rebuilding this later.

---

## 9. Non-Functional Requirements

### 9.1 Latency budget

Target **sub-700ms round-trip** (caller stops talking → agent starts responding), which is the widely-accepted ceiling before a call stops feeling like a real conversation. Rough allocation:

| Stage | Budget |
|---|---|
| STT (streaming, final transcript) | ~200–300ms |
| LLM time-to-first-token (Tier 1, non-reasoning) | ~200–400ms |
| TTS time-to-first-audio | ~40–100ms |
| Network/telephony overhead | ~50–100ms |

If you add a reasoning-mode call, a router hop, or an unnecessary extra tool call into the live turn, you will blow this budget — this is the most common way a technically-correct voice agent still feels bad to talk to.

### 9.2 Concurrency

Design for your realistic peak (likely mid-morning/early-afternoon inbound spikes for a freight desk). Pipecat/LiveKit Agents both scale horizontally per-call — size your container autoscaling to your call-arrival distribution, not just monthly average volume.

### 9.3 Compliance — call recording consent

This is a real, non-optional requirement, not a nice-to-have: **US call recording consent laws vary by state.** Roughly a dozen states (including California, Florida, and several others) require **all-party consent** to record a call; the rest are one-party consent. Since inbound carrier calls can originate from any state, you need either:
- A recorded consent disclosure at call start ("this call may be recorded for quality purposes") played before any substantive conversation happens, applied uniformly regardless of caller state — the standard industry practice — or
- Legal sign-off on your specific approach

Loop in counsel on this before go-live; it's a cheap fix early and an expensive one to retrofit.

### 9.4 Adversarial input handling (this is the section worth reading closely given your background)

A voice negotiation agent is, structurally, an LLM-backed system that a hostile counterparty gets to talk to directly and repeatedly, with a financial incentive to manipulate it. Treat this as a prompt-injection / social-engineering surface, not just a UX problem:

- **The LLM must never be the sole authority that approves a price.** Every "accept this offer" path must call a function that is validated server-side against the live TMS floor for that specific Load ID, in code the LLM cannot influence via conversation. If the model is convinced by a clever caller that the floor is $200 lower than it actually is, the server-side check catches it regardless of what the model "believed."
- **Treat the system prompt as public.** Assume a determined caller may eventually extract or guess parts of your negotiation script (e.g., "what's the most you can offer?" repeated with rephrasing). Design the floor/ceiling enforcement so that knowing the strategy doesn't help a caller beat it — the number itself, not just the instruction not to reveal it, is the actual control.
- **Log every negotiation round with the raw transcript.** If a booking ever does complete below floor (bug or exploit), you need to be able to reconstruct exactly which turn caused it.
- **Rate-limit and flag repeat callers/MC numbers that probe the negotiation boundary** across multiple calls — a single call being manipulated is a bug; the same MC number calling back five times with different framings to find the floor is a pattern worth a fraud-review flag.

### 9.5 Failure modes to design for explicitly

- Transport Pro API/EDI is unreachable mid-call → don't leave the caller hanging; have a graceful "let me get someone to call you back" path with a logged callback task
- Assigned carrier sales rep doesn't answer the warm transfer → defined fallback (next rep in a round-robin, or a voicemail-plus-callback-task, never a dead-air disconnect)
- Carrier verification service is down (including FMCSA per §8.1) → fail safe to a human-review queue, not an auto-approve

---

## 10. Data Model (core entities)

| Entity | Key fields |
|---|---|
| `Load` | load_id, origin, destination, pickup_date, posted_rate, floor_rate, ceiling_rate, assigned_rep_id, status (mirrored from Transport Pro) |
| `Carrier` | mc_number, usdot_number, legal_name, authority_status, verification_risk_flags, last_verified_at |
| `Call` | call_id, load_id (nullable until identified), carrier_id (nullable until verified), start_time, end_time, outcome (booked / transferred / abandoned / rejected), transcript_ref |
| `NegotiationOffer` | call_id, round_number, offered_by (carrier/agent), amount, timestamp |
| `TransferEvent` | call_id, rep_id, transfer_result (connected / voicemail / failed), timestamp |

---

## 11. Team & Roles

| Role | Allocation | Responsibility |
|---|---|---|
| Voice AI / real-time systems engineer (lead) | Full-time | Orchestration framework, STT/LLM/TTS pipeline, latency tuning |
| Backend engineer | Full-time | Transport Pro integration, verification service integration, business logic API, data model |
| DevOps/infra engineer | Part-time (~0.5 FTE) | Hosting, IaC, CI/CD, observability |
| Conversation designer / prompt engineer | Full-time during build, part-time after | Call scripts, negotiation prompt design, eval harness, ongoing tuning against real call data |
| QA / eval engineer | Part-time, ramps up before each release | Regression test suite of simulated calls (§5.8) |

This roughly matches the "3–5 senior engineers" figure cited across industry build-cost benchmarks for a production-grade voice AI system — see cost section below.

---

## 12. Phased Rollout Plan

| Phase | Duration | Scope | Exit criteria |
|---|---|---|---|
| **0 — Discovery** | 2 weeks | Confirm Transport Pro integration path (§6), pick primary telephony/STT/TTS/LLM vendors, stand up dev environment | Written confirmation from Transport Pro on API/EDI access; vendor accounts provisioned |
| **1 — MVP** | 6–8 weeks | Steps 1–4 of the call flow working end-to-end against **sandbox/test load data**; basic negotiation (single round); no live transfer yet (log intent instead) | A test call can identify a load, verify a test MC number, negotiate one round, and log the outcome correctly |
| **2 — Hardening** | 4–6 weeks | Full negotiation loop with floor/ceiling enforcement (§9.4), live warm transfer with fallback handling (§9.5), call-recording consent flow (§9.3), eval harness with adversarial test cases | Passes internal eval suite; zero floor violations across 500+ simulated adversarial calls |
| **3 — Pilot** | 4 weeks | Route a **subset** of real inbound lines to the agent; human team monitors every call initially, then a sample | Defined success metrics hit (see §13) on real traffic for 2+ consecutive weeks |
| **4 — Production rollout** | Ongoing | Expand to full inbound volume; begin cost optimization (model tiering refinement, telephony renegotiation, self-hosting evaluation per §7.3) | — |

**Total to a real pilot: roughly 4–5 months**, which is in line with (in fact slightly faster than) the 4–9 month range commonly cited for enterprise-grade in-house voice AI builds — achievable because you're scoping tightly to one call flow rather than a general-purpose platform. Budget contingency: internal builds in this space commonly land at **roughly 2x the original engineering estimate**, almost always because of underbudgeted edge-case handling (turn-taking, noisy lines, adversarial negotiation) rather than the core pipeline — plan your timeline and budget with that multiplier in mind, not as a worst case but as a base case.

---

## 13. Success Metrics

| Metric | Target (set after pilot baseline) |
|---|---|
| Call answer rate (no busy/voicemail during business hours) | 100% |
| % of calls resolved without human involvement (booked or cleanly declined) | Baseline in pilot, improve over time |
| % of failed negotiations correctly transferred to the right rep | >95% |
| Floor-price violations | 0 (structural requirement, not an aspiration) |
| Average round-trip latency (P50 / P95) | <700ms / <1200ms |
| MC/DOT verification false-negative rate (valid carrier incorrectly blocked) | Track and minimize — false negatives cost you real freight coverage |
| Carrier sentiment / complaint rate | Track via post-call survey or rep feedback |

---

## 14. Risks & Open Questions

| Risk | Mitigation |
|---|---|
| Transport Pro has no self-serve real-time API (EDI-only model) | Confirm in Phase 0 (§6); fall back to a near-real-time data mirror if needed |
| FMCSA QCMobile API reliability (currently reporting an extended outage) | Commercial fallback verification service (§8.2); never single-source this |
| Negotiation logic manipulated by an adversarial caller | Server-side deterministic floor enforcement (§9.4) — non-negotiable design requirement |
| Call-recording consent law violation | Legal review before go-live (§9.3) |
| Engineering timeline/budget overrun | Plan for the 2x rule from the start (§12); scope MVP tightly |
| Team hiring — real-time voice AI + telephony experience is a narrow skill set | Consider a specialist contractor/agency for the initial pipeline build (STT/LLM/TTS orchestration), keep the Transport Pro integration and negotiation logic in-house since that's your actual IP |

---

## Sources
- pipecat.ai, github.com (Pipecat, LiveKit Agents, TEN, Bolna repos) via futureagi.com, thinnest.ai, techsy.io, inworld.ai, cekura.ai, arunbaby.com — framework comparisons
- retellai.com, softcery.com, wildrunai.com, relinns.com — LLM latency/model selection for voice agents
- deepgram.com, futureagi.com, assemblyai.com, coval.ai, gradium.ai, teamday.ai — STT/TTS provider comparisons
- forasoft.com, telnyx.com, burki.dev, techsy.io, bitcall.io, callsphere.ai, viirtue.com — telephony/SIP provider comparisons
- openrouter.ai, betonai.net, costgoat.com, aipricingmaster.com, airealist.org — OpenRouter and self-hosting economics
- mobile.fmcsa.dot.gov, verifycarrier.com, cargotools.online — FMCSA QCMobile API access and current status
- transportpro.net, zenbridge.io, tai-software.com — TMS EDI/API integration patterns
- techsy.io, bitbytes.io, groovyweb.co, haptik.ai — build cost/timeline benchmarks (carried over from prior report)
