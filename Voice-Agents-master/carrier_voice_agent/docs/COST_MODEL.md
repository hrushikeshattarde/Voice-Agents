# What LaneVoice costs to run — end to end

Every dollar the system spends, from the moment a carrier dials the number to the
moment a rep picks up on 8x8, plus the AWS bill underneath it.

**Headline:** a 5-minute AI call costs **~18¢**. A call that ends in a transfer
costs **~26¢**. Blended at a 25% transfer rate, **~20¢ per call**. At 2,000 calls
a month the whole system — telephony, AI, AWS, everything — runs about
**$550/month**, or **28¢ per call all in**.

All rates verified against published pricing pages in **August 2026** and sourced
at the bottom. Anything I could not verify is marked ⚠️.

---

## 1. Method

**What's counted:** Twilio, LiveKit, OpenRouter (STT + LLM + TTS), AWS, and the
8x8 leg of a transfer. **What isn't:** Transport Pro and Highway subscriptions
(you already pay them; ⚠️ confirm neither meters per API call — the agent makes
roughly 6–10 Transport Pro calls per conversation), 8x8 seat licences, and
engineering time.

**The reference call** — every number below derives from this profile. Change
these and the model moves:

| Assumption | Value | Where it comes from |
|---|---|---|
| AI conversation length | 5.0 min | typical qualify → verify → negotiate → close |
| Agent speaking turns | 18 + greeting | one composed turn per caller turn |
| Agent speech volume | 3,200 characters | ~175 chars/turn at `LLM_MAX_TOKENS=220` |
| Caller speech reaching STT | 2.0 min | VAD-gated; the agent isn't billed for silence |
| `compose()` calls | 18 | one per turn ([composer.py:140](../src/lanevoice/voice/composer.py#L140)) |
| `read()` calls | 6 | extraction turns (MC, email, city, rate) |
| Rejected-and-recomposed turns | 2 | the `correction` path |
| Transfer rate | 25% of calls | ⚠️ your number; 13 code paths route to transfer |
| Rep conversation after transfer | 6.0 min | ⚠️ your number |

**Models in use** ([settings.py:106](../src/lanevoice/settings.py#L106)):
`openai/whisper-large-v3` (STT), `anthropic/claude-haiku-4.5` (composer),
`microsoft/mai-voice-2-flash` (TTS) — all via OpenRouter.

---

## 2. One call, itemized

### 2a. The AI hops

| Hop | Volume | Rate | Cost |
|---|---|---|---|
| **STT** — Whisper large-v3 | 2.0 min audio | $0.0015 / min | **$0.0030** |
| **LLM in** — Haiku 4.5 | 37,000 tokens | $1.00 / MTok | **$0.0370** |
| **LLM out** — Haiku 4.5 | 1,450 tokens | $5.00 / MTok | **$0.0073** |
| **TTS** — MAI-Voice-2-flash | 3,200 characters | $15.00 / M chars | **$0.0480** |
| | | **AI subtotal** | **$0.0953** |

Where the 37,000 input tokens go — this matters, because it is the one line with
real headroom:

```
_SYSTEM prompt        ~1,000 tok  ×  20 calls  =  20,000 tok   (54%)
dialogue-so-far       ~350 tok avg × 20 calls  =   7,000 tok   (19%)
FACTS block           ~200 tok    × 18 calls   =   3,600 tok   (10%)
DIRECTIVE             ~120 tok    × 18 calls   =   2,160 tok    (6%)
read() prompts + misc                          =   4,240 tok   (11%)
```

**Prompt caching does not help here, and it is worth knowing why.** The system
prompt is resent on every single turn — 54% of all input tokens — and caching it
is the obvious fix. But Claude Haiku 4.5's minimum cacheable prefix is **4,096
tokens**, and the entire prompt (system + dialogue + facts + directive) is only
~1,700. Nothing is cacheable at any prompt position. A `cache_control` marker
would return `cache_creation_input_tokens: 0` with no error and no benefit. The
levers that do work are in §7.

### 2b. The telephony hops

| Hop | Volume | Rate | Cost |
|---|---|---|---|
| Twilio origination (PSTN → your trunk, US local) | 5.0 min | $0.0034 / min | **$0.0170** |
| LiveKit third-party SIP minutes | 5.0 min | $0.0040 / min | **$0.0200** |
| LiveKit agent session minutes | 5.0 min | $0.0100 / min | **$0.0500** |
| | | **Telephony subtotal** | **$0.0870** |

LiveKit rates are post-allowance (Ship: 5,000 SIP min included). The agent
session minute at $0.01 is the single largest telephony line — more than Twilio
and LiveKit SIP combined.

### 2c. The transfer, when it happens

Cold SIP REFER to the rep's 8x8 DID (see [TRANSFER_8X8.md](TRANSFER_8X8.md)).
Twilio bills a REFER'd **origination** call transferred to **PSTN** as mixed
origination + termination: $0.0034 + $0.0100 = **$0.0134/min** for the whole
human conversation. LiveKit and the AI stop billing at the handoff. 8x8 inbound
to a DID is $0 marginal (⚠️ confirm your plan includes unlimited US inbound).

| | |
|---|---|
| 6-minute rep conversation × $0.0134 | **$0.0804** |

### 2d. The bottom line per call

| Call type | Cost |
|---|---|
| AI handles it end to end (books, declines, no deal) | **$0.182** |
| AI hands off to a rep on 8x8 | **$0.263** |
| **Blended at 25% transfer rate** | **$0.202** |

```
AI-only call, 18.2¢:
  TTS            ████████████████████  4.8¢   26%
  LiveKit agent  ████████████████████  5.0¢   27%
  LLM            ██████████████████    4.4¢   24%
  LiveKit SIP    ████████              2.0¢   11%
  Twilio in      ███████               1.7¢    9%
  STT            █                     0.3¢    2%
```

Two things stand out: **TTS costs more than the LLM**, and **LiveKit costs more
than Twilio by 4×**. Neither is where people expect the money to be.

---

## 3. Unit rate card

Everything in one place, for re-checking against invoices.

### Telephony

| Item | Rate |
|---|---|
| Twilio origination — US local (inbound to your DID) | $0.0034 / min |
| Twilio origination — US toll-free | $0.0130 / min |
| Twilio termination — US 48 states (outbound) | $0.0100 / min |
| Twilio REFER'd origination → PSTN (a transfer) | $0.0134 / min (mixed) |
| Twilio initiating a REFER | free |
| Twilio local number | $1.15 / mo |
| 8x8 inbound to a DID or queue | $0 marginal ⚠️ |

### LiveKit Cloud

| Item | Build | Ship | Scale |
|---|---|---|---|
| Monthly base | $0 | from $50 | from $500 |
| Agent session minutes | $0.010 / min after allowance | $0.010 / min | $0.010 / min |
| Third-party SIP minutes | 1,000 incl. | 5,000 incl., then $0.004 | 50,000 incl., then $0.003 |
| WebRTC minutes | 5,000 incl. | 150,000 incl., then $0.0005 | 1.5M incl., then $0.0004 |
| Concurrent agent sessions | 5 | ⚠️ not published | 600+ |

### AI (OpenRouter)

| Item | Rate |
|---|---|
| `openai/whisper-large-v3` | $0.0015 / min of audio |
| `anthropic/claude-haiku-4.5` | $1.00 / MTok in · $5.00 / MTok out |
| `microsoft/mai-voice-2-flash` | $15.00 / M characters |
| Prompt-cache write / read (Haiku 4.5) | 1.25× / 0.1× — **unusable**, 4,096-tok minimum |
| OpenRouter credit-purchase fee | ⚠️ a payment-processing percentage applies on top-ups — check your invoice |

Switching the composer to `LLM_PROVIDER=anthropic` is already supported
([settings.py:53](../src/lanevoice/settings.py#L53)) and costs the same $1/$5 —
it removes the OpenRouter hop and its fee for the LLM, though STT and TTS still
go through OpenRouter either way.

### AWS (us-east-1)

| Item | Rate |
|---|---|
| EC2 c7g.xlarge (4 vCPU / 8 GB, Graviton3) | $0.145 / hr on-demand · $0.096 / hr 1-yr RI |
| EC2 c7i.xlarge (4 vCPU / 8 GB, Intel) | $0.179 / hr on-demand · $0.118 / hr 1-yr RI |
| Fargate ARM | $0.03238 / vCPU-hr · $0.00356 / GB-hr |
| Fargate x86 | $0.04048 / vCPU-hr · $0.004446 / GB-hr |
| EBS gp3 | $0.08 / GB-mo (3,000 IOPS + 125 MB/s free) |
| EBS snapshots (standard) | $0.05 / GB-mo |
| Public IPv4 address | $0.005 / hr ($3.65 / mo) |
| Data transfer out to internet | first 100 GB/mo free, then $0.09 / GB |
| CloudWatch Logs | $0.50 / GB ingest · $0.03 / GB-mo storage |
| ECR storage | $0.10 / GB-mo |
| Secrets Manager | $0.40 / secret-mo + $0.05 / 10k calls |
| SSM Parameter Store (standard) | free |
| RDS db.t4g.micro Postgres | ~$12–15 / mo incl. 20 GB ⚠️ verify |

---

## 4. AWS deployment — architecture and bill

### What actually has to run

One process: `lanevoice-worker start`. It **connects outbound** to LiveKit Cloud
and auto-joins `call-*` rooms — **no inbound ports, no load balancer, no public
endpoint**. That single fact removes an ALB ($16+/mo), a NAT gateway ($32/mo +
data), and most of a security review from the bill.

**No GPU.** STT, LLM and TTS all run on OpenRouter; only Silero VAD and the
BVCTelephony noise canceller run locally, both on CPU
([worker.py:101](../src/lanevoice/telephony/worker.py#L101)). ⚠️ The top of
[LIVE_SETUP.md](LIVE_SETUP.md) still recommends a GPU VM — that advice predates
the move to OpenRouter and contradicts Part C1 of the same document. Worth
deleting before someone provisions a g5.

**Sizing.** LiveKit's guidance is 4 cores / 8 GB per agent server, handling
10–25 concurrent sessions. Lean to the low end here: BVC noise cancellation is
CPU-heavy per session. Call it **10 concurrent calls per c7g.xlarge**.

Concurrency, not call volume, sizes the fleet:

| Calls / month | Avg concurrent (9h day, 22 days) | Peak at 3× | Instances |
|---|---|---|---|
| 500 | 0.2 | 1 | 1 |
| 2,000 | 0.8 | 3 | 1 |
| 10,000 | 4.2 | 13 | 2 |

### Option 1 — Single EC2 instance (start here)

| Line item | Monthly |
|---|---|
| c7g.xlarge, 24/7 on-demand (730 hr × $0.145) | $105.85 |
| EBS gp3 root, 30 GB | $2.40 |
| EBS snapshots, 30 GB daily-rotated | $1.50 |
| Public IPv4 × 1 | $3.65 |
| CloudWatch Logs (~3 GB ingest) | $1.60 |
| ECR (2 GB image) | $0.20 |
| SSM Parameter Store for secrets | $0.00 |
| Data transfer out (~5 GB — see below) | $0.00 |
| **Total, on-demand** | **$115.20** |
| **Total, 1-year Compute Savings Plan** | **$79.43** |

**Data transfer is genuinely negligible** and worth showing, because it is the
line people fear: the worker sends ~24–32 kbps Opus to LiveKit (~1.2 GB per
1,000 calls) and uploads caller audio to Whisper (~3 GB per 1,000 calls). You
stay inside the 100 GB free tier until roughly **20,000 calls/month**.

**Use SSM Parameter Store, not Secrets Manager.** Eight-ish credentials at
$0.40/secret-month is $3.20 for a feature this deployment doesn't need — no
rotation, no cross-account sharing. Standard Parameter Store with a KMS key is
free and adequate.

**IPv4 note:** $3.65/mo for an address the worker only uses for outbound. An
IPv6-only subnet with an egress-only gateway removes it — worth doing at fleet
scale, not worth the yak-shave for one box.

### Option 2 — ECS Fargate, business hours only

Freight desks are business-hours operations. A 24/7 instance idles ~65% of the
time.

| | Hours / mo | Monthly |
|---|---|---|
| Fargate ARM 4 vCPU / 8 GB, 24/7 | 730 | $115.34 |
| Fargate ARM, 12h × 22 days | 264 | $41.71 |
| EC2 c7g.xlarge with a start/stop schedule | 264 | $38.28 |

**The catch is real:** a carrier who calls at 9pm gets dead air, because no
worker is running to answer the room. Two ways to keep the savings honestly —
keep one `t4g.medium` up 24/7 (~$24.50/mo) purely for after-hours overflow, or
point the Twilio number at an 8x8 after-hours queue outside business hours. The
second is free and better.

### Option 3 — HA pair (the real production answer)

Two workers across AZs so a host failure doesn't take the phone line down.
**This forces one architectural change:** the audit database is a local SQLite
file ([database.py](../src/lanevoice/db/database.py)), holding calls,
`transfer_events`, and the rep directory. Two workers means two divergent
databases — a split audit trail, and a rep-availability table that disagrees
with itself. That breaks the 8x8 transfer feature specifically, since the rep
directory is what routes the call.

| Line item | Monthly |
|---|---|
| 2 × c7g.xlarge on-demand | $211.70 |
| RDS db.t4g.micro Postgres + 20 GB ⚠️ | $14.00 |
| EBS, snapshots, 2 × IPv4, logs, ECR | $16.60 |
| **Total, on-demand** | **$242.30** |
| **Total, 1-year Savings Plan on EC2** | **$170.50** |

Budget **1–2 engineering days** to move the repository from SQLite to Postgres.
The `Repository` class is already the single data-access seam, so it's a driver
swap plus schema migration, not a rewrite.

### Option 4 — Self-host LiveKit (don't, yet)

Eliminates $0.014/min of LiveKit Cloud charges by running the SFU + SIP bridge
on your own EC2. Break-even is around **15,000–20,000 media minutes/month**
(~3,000–4,000 calls) before ops effort — and self-hosting a media server means
owning TURN, egress bandwidth, SIP debugging, and upgrades. At 2,000 calls/month
LiveKit Cloud costs $140; a self-hosted pair costs more than that in EC2 alone.
Revisit above 5,000 calls/month.

---

## 5. Monthly totals at three volumes

Single-instance AWS on-demand, cold-transfer mode, 25% transfer rate.

### A — Pilot: 500 calls/month (one office)

| | Monthly |
|---|---|
| Variable (500 × $0.202) | $101.20 |
| less LiveKit Ship SIP allowance (2,500 min included) | −$10.00 |
| LiveKit Ship base | $50.00 |
| Twilio number | $1.15 |
| AWS (Option 1, on-demand) | $115.20 |
| **Total** | **$257.55** |
| **Per call** | **$0.52** |

At pilot volume you're mostly paying rent, not usage — fixed costs are 64% of
the bill. Switch to Option 2 (business-hours Fargate) and this drops to ~$184.

### B — Production: 2,000 calls/month

| | Monthly |
|---|---|
| Variable (2,000 × $0.202) | $404.80 |
| less LiveKit Ship SIP allowance | −$20.00 |
| LiveKit Ship base | $50.00 |
| Twilio number | $1.15 |
| AWS (Option 1, on-demand) | $115.20 |
| **Total** | **$551.15** |
| **Per call** | **$0.28** |

### C — Scale: 10,000 calls/month

| | Monthly |
|---|---|
| Variable (10,000 × $0.202) | $2,024.00 |
| less LiveKit Ship SIP allowance | −$20.00 |
| LiveKit base ⚠️ Ship, or Scale if concurrency exceeds Ship's cap | $50.00 – $500.00 |
| Twilio number | $1.15 |
| AWS (Option 3, HA pair on-demand) | $242.30 |
| **Total** | **$2,297 – $2,747** |
| **Per call** | **$0.23 – $0.27** |

Per-call cost falls with volume but only to a floor around 23¢ — 90% of the cost
is genuinely variable (minutes and tokens), so this doesn't have the economics
of a SaaS product. It has the economics of a phone bill, which is the right
mental model.

### What that buys

At scenario C, first-touch handling of 10,000 inbound carrier calls costs less
per month than one fully-loaded rep at any plausible salary. If 20% of calls
book, cost per booked load is **~$1.15** — against a load's margin, that is
rounding error. The system does not need to be cheap to be worth it; it happens
to be cheap anyway.

---

## 6. Cost of the 8x8 transfer feature specifically

From [TRANSFER_8X8.md](TRANSFER_8X8.md), for completeness in one place:

| Mechanism | Rate after handoff | 6-min transfer | 250 transfers/mo |
|---|---|---|---|
| Cold REFER (recommended) | $0.0134 / min | $0.080 | **$20** |
| Consult 45 s, then REFER | $0.0334 for 45 s, then $0.0134 | $0.105 | **$26** |
| Bridged the whole way | $0.0234–$0.0334 / min | $0.14–$0.20 | **$35–50** |

Adding warm transfers on the escalation paths costs about **$6/month** at 2,000
calls. That is not a budget decision; it's a UX decision.

---

## 7. What actually drives the bill

Ranked by dollars recoverable, at 2,000 calls/month.

| # | Lever | Saving / mo | Effort |
|---|---|---|---|
| 1 | **1-year Compute Savings Plan on EC2** | $36 | 10 minutes, no code |
| 2 | **Business-hours-only compute** + 8x8 after-hours queue | $75 | half a day |
| 3 | **Trim `_SYSTEM` by 30%** (~300 tok × 20 turns/call) | $12 | half a day + eval |
| 4 | **Shorter agent turns** — 20% fewer spoken characters | $19 | prompt tuning + eval |
| 5 | Graviton over Intel (c7g vs c7i) — already the default recommendation | $25 | none if you start right |
| 6 | Cold transfer instead of bridged | $15 | it's the default design |
| 7 | Self-host LiveKit | −$0 at this volume | weeks; don't |

**Levers 3 and 4 are the same lever seen twice**, and they compound: every word
the agent doesn't say is TTS characters not billed *and* dialogue tokens not
resent on all subsequent turns. A 20% reduction in agent verbosity is worth
~$31/month at 2,000 calls and, more importantly, makes the agent sound more like
a broker and less like a brochure. The prompt work pays for itself twice.

**The lever that isn't available:** prompt caching, blocked by Haiku 4.5's
4,096-token minimum (§2a). If the composer ever moves to Sonnet 5 (1,024-token
minimum) or Opus 5 (512), caching the system prompt becomes possible — but those
models cost 3× and 5× per token, so the caching saving would not come close to
paying for the model upgrade. Stay on Haiku; it is the right call for this
workload.

---

## 8. Before you commit — the ⚠️ list

Nine things in this document are assumptions rather than verified facts. In
rough order of how much they move the number:

1. **Transfer rate and rep talk time** (25% / 6 min) — your call data, not mine.
   Every transferred minute is $0.0134.
2. **Twilio REFER-to-PSTN billing.** I read "mixed origination/termination" as
   $0.0134/min. Confirm with Twilio support before enabling PSTN transfer — it
   is opt-in precisely because it has billing consequences.
3. **LiveKit Ship's concurrent-session cap** is not published. If scenario C
   exceeds it, add $450/month for Scale.
4. **LiveKit agent-session minute allowances** per plan are not published;
   §5 assumes none, so those totals are conservative.
5. **8x8 inbound is free on your plan.** Confirm unlimited US inbound on the
   seat licences.
6. **OpenRouter's credit-purchase fee** — a percentage applies on top-ups; read
   an invoice rather than the model pages.
7. **Transport Pro / Highway API metering.** The agent makes ~6–10 Transport Pro
   calls per conversation. If either meters per call, that's a real line item.
8. **RDS db.t4g.micro at ~$14/mo** — approximate; price it in the calculator for
   your region and backup retention.
9. **Token and character counts in §2a** are engineering estimates from reading
   the composer, not measurements. Run 20 real calls with per-turn `usage`
   logging and replace them — that single change makes this whole document
   authoritative instead of indicative.

Item 9 is the highest-value hour of work in this list. Everything downstream of
those numbers is arithmetic.

---

## Sources

- [Twilio — US SIP trunking pricing](https://www.twilio.com/en-us/sip-trunking/pricing/us)
- [Twilio — call transfer via SIP REFER](https://www.twilio.com/docs/sip-trunking/call-transfer)
- [LiveKit Cloud pricing](https://livekit.com/pricing)
- [LiveKit — self-hosted deployment sizing](https://docs.livekit.io/deploy/custom/deployments/)
- [OpenRouter — Whisper large-v3](https://openrouter.ai/openai/whisper-large-v3)
- [OpenRouter — MAI-Voice-2-Flash](https://openrouter.ai/microsoft/mai-voice-2-flash)
- Claude Haiku 4.5 pricing and prompt-cache minimums — Anthropic API reference (`claude-api` skill, cached 2026-06-24)
- [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/) · [AWS EBS pricing](https://aws.amazon.com/ebs/pricing/)
- EC2 on-demand and 1-year RI rates — [c7g.xlarge](https://instances.vantage.sh/aws/ec2/c7g.xlarge) · [c7i.xlarge](https://instances.vantage.sh/aws/ec2/c7i.xlarge)
