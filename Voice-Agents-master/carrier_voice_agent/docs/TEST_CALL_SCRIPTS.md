# Test Call Scripts — every scenario

Call your LiveKit number with the worker running (`uv run lanevoice-worker dev`)
and follow one script per call. Watch the terminal — you'll see
`GREETING → …`, `CALLER said → …`, `AGENT reply → …` for each turn.

## Speaking tips (important on CPU / phone audio)
- Say digits **slowly and one at a time**: "L… one… zero… zero… one." Wait for the agent to finish before you talk.
- To **accept** a rate, say: "yes", "okay", "that works", or "deal".
- To **reach a human**, say: "can I talk to a rep?"
- There's a ~2-second pause before the agent replies (the turn buffer) — that's normal; let it think.

## Sample data cheat-sheet
| Load | Lane | Agent opens at | Commits on its own up to | Max Buy (hard cap) | Fraud-low |
|---|---|---|---|---|---|
| **L1001** | Chicago → Dallas | $2000 | $2300 | $2500 | under $1400 |
| **L1002** | Atlanta → Miami | $1400 | $1670 | $1850 | under $1000 |
| **L1003** | LA → Phoenix | $900 | $1110 | $1250 | under $650 |
| L1004 | Newark → Boston | — | — | — | (already covered) |

Between the agent's own authority and Max Buy is a **human's** call: it won't pay
that itself, but it won't refuse you either — it warm-transfers. Above Max Buy is
the only genuine no-deal. L1002 also has special requirements it reads out and
asks you to confirm *before* it quotes a rate.

| MC / DOT number | Carrier | Authority | Result |
|---|---|---|---|
| **MC 123456** | Blue Sky Logistics | ACTIVE | ✅ proceeds |
| **MC 654321** | Roadrunner Freight | ACTIVE | ✅ proceeds |
| **MC 999888** | Ghost Carrier | SUSPENDED | ❌ blocked → review |
| **MC 555444** | Dormant Transport | INACTIVE | ❌ blocked → review |
| **MC 777111** | Reactivated Haulers | ACTIVE | ⚠️ risk flag → transfer |
| **MC 222333** | Banned Freight | ACTIVE | ❌ not approved → declined |
| (any unknown #) | — | — | ❌ not found → review |

**Only ACTIVE authority gets a rate.** That's the company requirement, so INACTIVE
and SUSPENDED are both hard stops — the call ends before any load detail or number
is discussed, and the reason is logged rather than explained to the caller. Any
status the vetting feed sends that we don't recognise is read as SUSPENDED, never
guessed as ACTIVE.

---

## The call order (this changed)

```
greeting → load/reference number → MC or USDOT → WHERE AND WHEN THE TRUCK IS EMPTY
        → load details + rate → negotiate → confirm pickup → email → rate con
```

Two things to know before you dial:

**Nothing about the load comes out until the empty call is done.** The agent reads
your reference number back, takes your MC, verifies you, confirms your company
name, and then asks where your truck is getting empty and when. Only after that
does it tell you the lane, the dates, the commodity, the miles and its number. If
you answer only half the question ("empty in Towson, Arizona") it asks for the
other half rather than re-asking the whole thing.

**There are no scripted replies.** Every line the agent says is written by the LLM
from the facts it's allowed to use, after reading what you actually just said — so
the wording will differ every call, and it will answer questions you throw in
sideways ("how much does it weigh?", "when's it deliver?") instead of ploughing
on. What it cannot do is choose a number: the negotiation engine decides every
rate, and a reply naming any figure it wasn't given is rejected and re-prompted.
Ask it something outside its facts — detention, lumpers, payment terms — and it
should say it'll check, not invent an answer.

> **Booking is a three-step close.** Agree on a rate → the agent confirms the
> pickup → **you** give the email for the rate con, then it locks it in. If you
> say you *can't* make the pickup, it hands you to a rep instead.

## Scenario 1 — Happy path: accept the opening offer
**Purpose:** confirm the whole flow works end to end, including the email check.

Wording will vary — assert on what it *does*, not how it says it.

| You say | Agent should |
|---|---|
| "I'm calling about load **L one zero zero one**." | Read **L1001** back digit by digit to confirm, then ask for MC/USDOT. Says **nothing** about the lane yet |
| "My MC is **one two three four five six**." | Check your company name back ("Blue Sky Logistics?"), then ask **where your truck is getting empty and when** |
| "**Empty in Dallas, Texas today.**" | *Now* the load comes out: full truckload Chicago to Dallas, picks up with its window, delivers with its window, packaged food goods, 26 pieces, 42,000 lbs, dry van, ~925 miles — then "I've got it at **$2000**", then asks if you want it |
| "**Yes, that works.**" | Confirms the rate, asks if you can cover that pickup |
| "**Yep, I can cover it.**" | Asks which email to send the rate con to, and suggests *nothing* |
| "**Billing at blue sky logistics dot com.**" | Checks it against the carrier's file, confirms booked at **$2000**, and names the address the con is going to |

**Expected outcome:** `booked` at $2000, rate con addressed to the email *you*
gave. The agent asks about the email only — no driver or truck questions.

Worth trying mid-call: ask "**how many miles is it?**" or "**when's it deliver?**"
after the rundown. It should answer from the load's facts and then get back to
what it was doing. Ask "**do you offer quick pay?**" and it should say it'll check
rather than making terms up.

### How the email is handled

Every carrier has **several** addresses on file (dispatch, billing, after-hours).
Whatever you say is checked against that list:

* **An address already on file** → matched, used, nothing changes.
* **A new address** ("booking **at** blue sky freight **dot** com") → used *and
  appended* to the carrier's file, alongside the ones already there. The next
  call already knows it. Spoken form is parsed, so you never have to spell it.
* **"Just use the one you've got"** → it takes the most recent address on file
  rather than stalling.
* **No usable address after two tries** → it books the load but records
  `NOT CAPTURED — needs follow-up before sending`, and drops the "sign it"
  line entirely. It never invents an address.

Inspect the file at any point with:

```bash
sqlite3 carrier_agent.db "SELECT usdot_number, email FROM carrier_emails ORDER BY usdot_number, id"
```

---

## Scenario 2 — Negotiate: make the carrier do the walking, then close once
**Purpose:** the agent anchors LOW and **never answers your concession with a
concession of its own**. Every time you come down it credits the move, restates
its own number and asks how close you can get — a carrier who is still coming
down can usually come down again. Only when you stop moving (or tell it your
number is your best) does it put money on the table, and then it spends **once**,
decisively.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." (then MC, then the empty call) | Verify, give the full load rundown, ask **$2000** |
| "I need **twenty-five hundred**." | **Hold + discovery:** can't get to $2500, it's at **$2000**, and — because it took your empty call — it references *where your truck already is* rather than asking again, then asks how close you can get to $2000 |
| "**Still twenty-five hundred.**" (no movement) | **Holds again and sells the load,** not the rate — one non-price point, then "what's the best you can actually do?" Offers *nothing* new |
| "**Twenty-four hundred.**" (you come down $100) | **Asks, doesn't pay:** "Okay, $2400's better — but I'm still at **$2000**. How close can you actually get to me on it?" |
| "**Twenty-three hundred.**" | **Asks again**, with a *different* selling point: "You're coming my way, but $2300's still short — I'm at **$2000**. Where do you actually need to be on it?" |
| "**Twenty-two hundred.**" | **Its one closing move:** "Let's not go back and forth on it. I'll come to **$2100**, and I'll book you right now at that." |
| "**Twenty-two hundred, final.**" | "Final" ends the asking → **books at $2200** → confirms pickup + rate con |

**Expected outcome:** `booked` at **$2200**, under the hidden $2500 Max Buy — and
the agent named only **two** numbers all call ($2000 and $2100). Watch for:

* **No ladder.** It will not go $2000 → $2050 → $2100 → $2150 chasing you down.
  Its own number stays put until it closes.
* Repeating the same number earns you nothing — and *moving* earns you a
  question, not a counter-offer.
* Say "**that's my best**", "**2400 firm**", "**no less**" or "**take it or
  leave it**" and it stops asking and makes its move. Withhold that and it keeps
  pushing you — which is the point.
* The agent never walks away from a rate it's cleared to pay.
* Sit immovably on **$2400** (inside Max Buy, above the agent's own $2300
  authority) and it does *not* cave and does *not* refuse — best-and-final at
  **$2150**, then a human: "that's above what I can approve on my own, but it's
  not a no."
* Sit on **$2800** (above Max Buy) and after the **$2150** best-and-final it
  declines with a note, leaving the door open for next time.

Tune firmness with `NEGOTIATION_MAX_PULLS` (how many times it asks you to come
closer before spending anything — raise it to squeeze harder, `0` to close on
your first move), `NEGOTIATION_RECIPROCITY` (how much of the remaining gap its
one closing move covers), `NEGOTIATION_DISCRETION_RATE` (how far it commits
without a human), `NEGOTIATION_SPLIT_GAP_RATE` / `NEGOTIATION_SETTLE_GAP_RATE`
(how eagerly it closes), and `NEGOTIATION_MAX_HOLDS`.

---

## Scenario 3 — Carrier asks cheap → instant accept
**Purpose:** a below-offer ask is great for us, so we take it.

| You say | Agent should |
|---|---|
| "Load **L one zero zero two**." | Read the number back, ask MC |
| "MC **six five four three two one**." | Ask the empty call |
| "**Empty in Atlanta, Georgia today.**" | Give the load, read out the **zero-degree reefer + strict 8 AM** requirement, ask if you can do it |
| "**Yeah, I can run it that cold.**" | *Now* quotes **$1400** |
| "I'll do it for **thirteen fifty**." | Takes it → confirms pickup + rate con |
| "**Yep, I can cover it.**" | Books at **$1350** |

**Expected outcome:** `booked` at $1350.

---

## Scenario 4 — Ask way too high → hold, walk up, then hang up with a note
**Purpose:** the agent never overpays; it walks away politely and logs a note.
Keep repeating the high number when it counters.

| You say | Agent should |
|---|---|
| "Load **L one zero zero three**." | Read the number back, ask MC/USDOT |
| "MC **six five four three two one**." | Ask the empty call |
| "**Empty in Ontario, California right now.**" | Give the load (LA to Phoenix, flatbed, steel tubing, ~375 mi), ask **$900** |
| "I need **fifteen hundred**." | **Hold + discovery:** "$1500's a reach — I'm at $900… how close can you get to $900?" |
| "**Fifteen hundred**, no less." | **Holds again**, sells the load, asks for your best — still **$900** |
| "Still **fifteen hundred**." | **Best-and-final $1005** — note it is *not* the full $1110 it could authorise, because you never moved |
| "Still **fifteen hundred**." | "I've come up as far as I can… I can't make it work today." → **ends call** |

**Expected outcome:** `no_deal` in 4 negotiation turns, and a note in the DB:
*"NO DEAL on L1003… carrier held at $1500, which is ABOVE Max Buy $1250…"*.
Stonewalling deliberately earns you **less** than negotiating would have
(`NEGOTIATION_STONEWALL_FINAL_RATE`) — come down even once and the best-and-final
is bigger.

---

## Scenario 5 — Suspiciously cheap offer → fraud review
**Purpose:** an absurdly low rate is a fraud red flag, not a bargain.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." (then MC, then the empty call) | Give the load, ask **$2000** |
| "I'll haul it for **nine hundred**." | "Let me connect you with **Sarah Chen**…" (routed to review) |

**Expected outcome:** `transferred` (fraud review); note logged.

---

## Scenario 6 — Non-ACTIVE authority → blocked, not hung up
**Purpose:** only ACTIVE authority gets a rate. Try **MC 999888** (SUSPENDED) and
**MC 555444** (INACTIVE) — both stop here, before any load detail or number.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Read the number back, ask MC |
| "MC **nine nine nine eight eight eight**." | "I'm not able to verify active authority and insurance… routing this to our team for review." |

**Expected outcome:** `rejected` (human review).

---

## Scenario 7 — Risky carrier (recently reactivated) → straight to a human
**Purpose:** verified but flagged → hand to a rep to finish verification.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Read the number back, ask MC |
| "MC **seven seven seven one one one**." | "I want to get you to a rep to finish verification — connecting you…" |

**Expected outcome:** `transferred`.

---

## Scenario 8 — Load not found → offers open loads
| You say | Agent should |
|---|---|
| "I want load **L nine nine nine nine**." | "I couldn't find L9999. Open loads are L1001, L1002, L1003. Which one?" |
| "**L one zero zero one**." | Continues normally (reads the number back, asks MC) |

---

## Scenario 9 — Load already covered
| You say | Agent should |
|---|---|
| "Load **L one zero zero four**." | "Sorry, L1004 is already covered. Other open loads: L1001, L1002, L1003." |

---

## Scenario 10 — Caller asks for a human
| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." (then MC, then the empty call) | Give the load, ask $2000 |
| "**Can I just talk to a rep?**" | "Let me connect you with **Sarah Chen**…" |

**Expected outcome:** `transferred` (carrier request).

---

## After a call — check what was logged
```bash
uv run python -c "import sqlite3; c=sqlite3.connect('carrier_agent.db'); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute('SELECT call_id,outcome,load_id FROM calls')]; print('--- notes ---'); [print(r['note']) for r in c.execute('SELECT note FROM call_notes')]"
```

## If the agent keeps saying "I didn't catch a load ID"
`openai/whisper-large-v3` is accurate, but spoken digits over phone audio can
still slip — and note that OpenRouter documents the transcription `prompt` field
as accepted and **ignored**, so the freight vocabulary in `STT_PROMPT` is not
biasing the model today. Say it slowly — **"load L, one, zero, zero, one."** The
agent already reads the number back digit by digit to confirm, and
`parsing.digit_readings` reconstructs a number a caller was cut off mid-way
through.
