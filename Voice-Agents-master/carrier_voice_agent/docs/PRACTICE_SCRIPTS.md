# Practice Scripts — test every persona and every path

Open the dashboard (`make dashboard`), go to **Practice**, enter your name and
follow one script per session. These exercise both sides of the feature: that
each customer *behaves* like their profile says, and that the scorecard,
recording and manager email land the way the docs promise.

Voice or text both work for these — the conversation logic is identical. Voice
additionally exercises STT, the vocal-delivery verdict and the call recording.

## Reading the results (important)

**There are no scripted replies and no scripted scores.** The customer's
wording differs every session, and rubric scores wobble a point or two between
runs — that's a model judging, not arithmetic. So assert on **direction and
behavior**, never exact numbers:

- Did the customer *reveal* something only after you asked a sharp question?
- Did the hangup trigger actually fire when you earned it?
- Is the goal verdict (✅/✗) right for what you actually secured?
- Are the "Work on" items pointing at real moments, with your verbatim quote?
- The **metrics row is exact** (talk ratio, questions, fillers/min) — those you
  CAN assert on precisely.

> A "win" needs something concrete: a date, a number taken with a stated
> trigger, a named next step. "Sounds good, call me sometime" should score
> **Goal not reached** — if it doesn't, that's a bug worth reporting.

## Persona cheat-sheet

| Profile | Difficulty | What wins | Instant hangup |
|---|---|---|---|
| The Chatty Non-committer | Easy | a commitment with a DATE on it | rushing his stories twice; "I need a yes or no today" |
| The Loyal Incumbent | Easy | a named backup slot / kept one-pager | bad-mouthing his broker twice |
| The Brush-off | Medium | a scheduled call or his crunch timing + your number | monologue pitch; pushing past three "we're all set"s |
| The Gatekeeper | Medium | the decision maker's name + when to reach him | fake familiarity ("just following up with him") |
| The Rate Shopper | Medium | a trial load / quote WITH service terms | refusing to touch price after three asks |
| The Burned Shipper | Hard | one low-stakes test load or a reference call | "that would never happen with us" |
| The Busy Operator | Hard | a real time slot in her quiet window | pitching past her sixty seconds |
| The Skeptical Negotiator | Hard | invited to compete / meet his ops team | a made-up statistic; folding on your own numbers |

---

## Worked example 1 — The Brush-off, played WELL (expect: goal ✅)

**Purpose:** prove discovery earns hidden facts, and a concrete close wins.

| You say | The customer should |
|---|---|
| "Hey Dale, this is ⟨you⟩ with Circle Logistics. I know I'm the tenth broker call today, so one question and I'm gone: when the fab line pushes everything out at quarter-end, who covers the trucks your regulars can't?" | NOT hang up — the pattern interrupt bought you time. Something guarded like "we've got a broker for overflow… why?" — he reveals the overflow arrangement because you asked a specific question |
| "Fair enough — sounds like they've earned it. When that overflow guy is slow to answer after five, what happens to the load?" | Reveal the pain (loads sit, night phone calls) — this is a hidden fact you EARNED. Watch that he gives it reluctantly, not as a monologue |
| "That's the gap we staff for — our night desk answers in two rings. How about this: I won't pitch you again. Take my cell, and next quarter-end crunch, if you've got a flatbed uncovered at 7 PM, call me once. Fair?" | Concede — take the number, perhaps grudgingly ("no promises"). That IS the win condition |

Then **End call**. Expect: goal **achieved**, opening/composure/closing scored
high, and at least one honest "Work on" (this script never asks about his
carrier's tarping problem — the judge should catch that you left it unearned).

## Worked example 2 — The Brush-off, played BADLY (expect: hangup)

**Purpose:** prove the hangup triggers fire and the scorecard says why.

| You say | The customer should |
|---|---|
| "Hi Dale! How's your day going? I'm with Circle Logistics, a full-service third-party logistics provider offering flatbed, van and reefer capacity across all 48 states with industry-leading service and competitive rates…" (keep going, two long turns of pitch, no questions) | Cut you off politely ("lemme stop you there"), say some form of "we're all set" |
| Keep pitching anyway: "We also have a night dispatch desk, load tracking, and dedicated account management…" | Colder. Second or third "we're all set" |
| "But our rates are really competitive, can I just send you a packet?" | **Hang up** — final line + click. Session ends with "The customer hung up" |

Expect: goal **not reached**, `hung_up_on: true` in the metrics, low listening
and discovery scores, and "Work on" items quoting your own monologue back at
you with better lines.

---

## Quick scripts — the other six

One strong-play sketch each. The **bold move** is the one the persona is built
to reward — skip it and you should feel the difference in the scorecard.

**The Rate Shopper (Marcy).** She opens demanding a number. Don't dodge three
times (that's her hangup trigger) — acknowledge it, then **ask what the cheap
carriers cost her**: "Before I throw a number — the cheapest guy on your
spreadsheet, how's he doing on Friday afternoon pickups?" She'll reveal the
missed-Friday fines if you dig. Close on a trial load with a service
commitment. Saying only "we're cheaper" should lose; a straight "I won't be
your cheapest — here's what the spread buys" should hold her.

**The Burned Shipper (Ray).** He opens hostile: "I don't use brokers anymore."
**Ask what happened, then let the whole story finish** — interrupting it twice
is a hangup. Never say "that would never happen with us" (instant one-warning
territory); instead answer the 2 AM question concretely: who answers, what the
recovery playbook is, who pays. Ask for one non-critical test load, framed as
earning the right to more. Overpromise and you're done.

**The Busy Operator (Tanya).** You get sixty seconds and she keeps yelling at
the dock. **One breath of relevance, then ask for a better time**: "Thirty
seconds: we cover building-supply overflow when your regulars are full. When's
a calmer time today — after three?" A full pitch into the chaos gets cut off.
The win is a real time slot, not a maybe.

**The Chatty Non-committer (Gene).** He'll talk forever and agree with
everything, meaning none of it. Ride one story warmly, then **trial-close with
a date**: "Can I call you the first week of September about harvest overflow?"
"Call me down the road" is NOT a win — push gently for the named week. Rude
interruptions are the only way to lose him; vagueness just wastes the session.

**The Skeptical Negotiator (Victor).** Every claim gets cross-examined. Use
real numbers with honest caveats, and when he asks something you can't answer,
say **"I don't know — I'll get you the answer by tomorrow"** (he respects it
once). Never invent a statistic — he will quote it back and end the call. Hold
your numbers when pushed; folding ("well, maybe more like 90%") is a trigger.
Unprompted detail on carrier vetting and double-brokering prevention is the
move that opens him up.

**The Gatekeeper (Brittany).** She is the first customer, not an obstacle. Be
honest that it's a sales call, use her name, and **ask for her advice**:
"What's the best way to get fifteen minutes with whoever handles plant
shipping?" Earn it and she gives you the decision maker's name and his calling
window. Claim you "spoke with him last week" and you're permanently done.

---

## System checks — the machinery around the conversation

| Check | Do | Expect |
|---|---|---|
| Silent clip | Voice mode: hold the talk button, say nothing, release | "Couldn't make out any speech…" — session unharmed, re-record |
| Slipped click | Tap the talk button for <0.4s | Nothing sent at all |
| Turn cap | `PRACTICE_MAX_TURNS=2` in `.env`, restart, run 2 turns | Session ends "Turn limit reached", still scored |
| Text fallback | Deny the mic permission, start a voice session | Error note, session runs as typing |
| Recording | Finish any voice session | Player on the summary card and in Recent sessions → row; both sides audible in order |
| Clip retention | Finish a voice session, look in `practice_audio/<id>/` | Only `call.wav` remains (turn clips deleted); with `PRACTICE_KEEP_AUDIO=true`, all clips remain |
| No manager picked | Leave the dropdown on "Don't email the report" | No email status line anywhere; report columns stay null |
| Manager, no SMTP | Add a manager to `managers.toml`, leave `SMTP_HOST` unset | "Report not emailed: email not configured…" on the summary card — session and scorecard unharmed |
| Manager + SMTP | Configure `SMTP_HOST`/`SMTP_FROM` (+ login) | "📧 Report emailed to ⟨manager⟩" on the summary card; `emailed_at` in the report detail |
| Offline stub refusal | `USE_LLM=false` in `.env`, restart, start a session | 400 naming `USE_LLM` — practice never runs against the stub |
| Judge resilience | (hard to force — happens on gateway hiccups) | "Scorecard unavailable" with metrics still shown; transcript saved; the row records the error |

## What a session costs

Roughly a cent: one composer call per turn, one rubric-judge call and (voice)
one vocal-judge call at the end, plus STT/TTS per voice turn. The turn cap is
the ceiling on a tab someone forgot to close.
