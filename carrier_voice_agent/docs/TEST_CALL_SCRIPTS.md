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
| Load | Lane | Agent opens at | Won't pay above | Fraud-low |
|---|---|---|---|---|
| **L1001** | Chicago → Dallas | $2000 | $2350 | under $1400 |
| **L1002** | Atlanta → Miami | $1400 | $1700 | under $1000 |
| **L1003** | LA → Phoenix | $900 | $1100 | under $650 |
| L1004 | Newark → Boston | — | — | (already covered) |

| MC / DOT number | Carrier | Result |
|---|---|---|
| **MC 123456** | Blue Sky Logistics | ✅ verified |
| **MC 654321** | Roadrunner Freight | ✅ verified |
| **MC 999888** | Ghost Carrier | ❌ revoked → review |
| **MC 777111** | Reactivated Haulers | ⚠️ risk flag → transfer |
| (any unknown #) | — | ❌ not found → review |

---

## Scenario 1 — Happy path: accept the opening offer
**Purpose:** confirm the whole flow works end to end.

| You say | Agent should |
|---|---|
| "I'm calling about load **L one zero zero one**." | Confirm *Chicago to Dallas, Dry Van*, ask for MC/USDOT |
| "My MC is **one two three four five six**." | "You're verified, Blue Sky Logistics. I've got this at **$2000**. Does that work?" |
| "**Yes, that works.**" | "Done — you're booked on L1001 at **$2000**…" |

**Expected outcome:** `booked` at $2000.

---

## Scenario 2 — Negotiate: agent holds firm, then makes real concessions
**Purpose:** see the human hold-then-concede behavior (bigger, decreasing moves
that converge toward the cap, not tiny fixed steps).

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Confirm lane, ask MC |
| "MC **one two three four five six**." | Offer **$2000** |
| "I need **twenty-three hundred**." | **Hold:** pushes back, reacts to $2300, stays at $2000 |
| "**Twenty-three hundred.**" | **Concede:** comes up to **~$2175** |
| "**Still twenty-three hundred.**" | **Concede:** comes up to **~$2280** |
| "**Twenty-three hundred.**" | **Books at $2300** (now within its cap) |

**Expected outcome:** `booked` at $2300 (a rate at/under the hidden $2350 cap).
If you ask *above* $2350 and hold, it makes its best offer, then declines with a note.

---

## Scenario 3 — Carrier asks cheap → instant accept
**Purpose:** a below-offer ask is great for us, so we take it.

| You say | Agent should |
|---|---|
| "Load **L one zero zero two**." | Confirm *Atlanta to Miami, Reefer*, ask MC |
| "MC **six five four three two one**." | Offer **$1400** |
| "I'll do it for **thirteen fifty**." | Books at **$1350** (cheaper than our offer) |

**Expected outcome:** `booked` at $1350.

---

## Scenario 4 — Ask way too high → hold, walk up, then hang up with a note
**Purpose:** the agent never overpays; it walks away politely and logs a note.
Keep repeating the high number when it counters.

| You say | Agent should |
|---|---|
| "Load **L one zero zero three**." | Confirm *LA to Phoenix, Flatbed*, ask MC |
| "MC **six five four three two one**." | Offer **$900** |
| "I need **fifteen hundred**." | **Hold:** "…I've got it at $900…" |
| "**Fifteen hundred**, no less." | Walk up: "…come up to $930…" |
| "Still **fifteen hundred**." | Walk up: "…$960…" |
| (keep refusing ~2 more times) | Eventually: "I've come up as far as I can… I won't be able to make it work today." → **ends call** |

**Expected outcome:** `no_deal`, and a note in the DB: *"NO DEAL on L1003… carrier held at $1500… walked up to $… above ceiling."*

---

## Scenario 5 — Suspiciously cheap offer → fraud review
**Purpose:** an absurdly low rate is a fraud red flag, not a bargain.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Confirm lane, ask MC |
| "MC **one two three four five six**." | Offer **$2000** |
| "I'll haul it for **nine hundred**." | "Let me connect you with **Sarah Chen**…" (routed to review) |

**Expected outcome:** `transferred` (fraud review); note logged.

---

## Scenario 6 — Revoked authority → blocked, not hung up
**Purpose:** a bad carrier can't negotiate, but is sent to review, not dropped.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Confirm lane, ask MC |
| "MC **nine nine nine eight eight eight**." | "I'm not able to verify active authority and insurance… routing this to our team for review." |

**Expected outcome:** `rejected` (human review).

---

## Scenario 7 — Risky carrier (recently reactivated) → straight to a human
**Purpose:** verified but flagged → hand to a rep to finish verification.

| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Confirm lane, ask MC |
| "MC **seven seven seven one one one**." | "I want to get you to a rep to finish verification — connecting you…" |

**Expected outcome:** `transferred`.

---

## Scenario 8 — Load not found → offers open loads
| You say | Agent should |
|---|---|
| "I want load **L nine nine nine nine**." | "I couldn't find L9999. Open loads are L1001, L1002, L1003. Which one?" |
| "**L one zero zero one**." | Continues normally (confirm lane, ask MC) |

---

## Scenario 9 — Load already covered
| You say | Agent should |
|---|---|
| "Load **L one zero zero four**." | "Sorry, L1004 is already covered. Other open loads: L1001, L1002, L1003." |

---

## Scenario 10 — Caller asks for a human
| You say | Agent should |
|---|---|
| "Load **L one zero zero one**." | Confirm lane, ask MC |
| "MC **one two three four five six**." | Offer $2000 |
| "**Can I just talk to a rep?**" | "Let me connect you with **Sarah Chen**…" |

**Expected outcome:** `transferred` (carrier request).

---

## After a call — check what was logged
```bash
uv run python -c "import sqlite3; c=sqlite3.connect('carrier_agent.db'); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute('SELECT call_id,outcome,load_id FROM calls')]; print('--- notes ---'); [print(r['note']) for r in c.execute('SELECT note FROM call_notes')]"
```

## If the agent keeps saying "I didn't catch a load ID"
Groq's `whisper-large-v3-turbo` is accurate, but spoken digits over phone audio
can still slip. Say it slowly — **"load L, one, zero, zero, one."** If a specific
number is consistently misheard, tell me and I'll add digit-biasing + a read-back
confirmation step.
