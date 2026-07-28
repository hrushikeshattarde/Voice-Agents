# Transport Pro integration

Loads, carrier vetting and contact addresses come from the Transport Pro Public
API (Circle Logistics tenant). This document is the map: which endpoint answers
which question, how its fields become the things the agent says, which loads the
agent is allowed to sell, and the assumptions that need checking against live
data.

```
DATA_SOURCE=transportpro     the live API (default)
DATA_SOURCE=sqlite           the offline seed data — demo and tests
```

Both modes open the local SQLite database. Under Transport Pro it holds the
**call audit trail** (calls, offers, notes, handoffs) and the warm-transfer rep
list, because the Public API has no endpoint for either, and losing the audit
trail would make a disputed booking unauditable.

---

## Before go-live

```bash
lanevoice-tpcheck --load 1303369 --mc 343195 --raw
```

Read-only — it authenticates, looks the two records up, and prints the raw
payload next to what the mappers made of it. **Three things to confirm**, all
called out below and all flagged in the tool's own output:

1. **The load status vocabulary.** The agent sells only `Ready To Dispatch` by
   default, and the collection's Voice AI example answers `AVAILABLE`. If the tool
   says `is NOT sellable`, set `TRANSPORT_PRO_OPEN_LOAD_STATUSES` to the value it
   printed. See *Which loads get sold*.
2. **`carrier_status` field names.** If the output says
   `NO STATUS FIELD FOUND`, add the real field name to `_STATUS_KEYS` in
   [`mappers.py`](../src/lanevoice/integrations/transportpro/mappers.py).
3. **Appointment timestamps.** Check a pickup window in `--raw` against what
   the same load shows in the Transport Pro UI. See *Timestamps* below.

---

## Endpoints

| Question the call is asking | Endpoint |
|---|---|
| authenticate | `POST /auth` (HTTP Basic, **no body**), refresh via the same path with a JSON body |
| is this load number real, and is it sellable? | `GET /load/{id}` |
| what else is open, to offer by number? | `GET /load/search?loadStatus=&isPosted=true` |
| is this MC/USDOT active with us? | `GET /voiceai/carrier_status?mc_number=` / `?dot_number=` |
| what addresses are on their account? | `GET /contact/search?connnectionRecordType=brokerCarrier&connectionRecordId=` |
| record the agreed rate | `POST /voiceai/load/{id}/make_offer` |
| write the call outcome onto the load | `POST /voiceai/load/{id}/add_note` |
| log a truck we couldn't use *(available, not wired — see below)* | `POST /voiceai/add_carrier_capacity` |

Two details that look like mistakes and are not:

* `POST /auth` takes Basic credentials and **no request body**. The refresh call
  to the same path takes a JSON body and no Basic header.
* `/contact/search` really does spell it **`connnectionRecordType`**, with three
  n's. That is the wire format. Spelling it correctly returns nothing, every
  carrier then looks like they have no address on file, and the booking gate
  refuses everybody.

The load lookup is **one call**. `GET /load/{id}` answers everything the call
needs at once — whether the load exists, `status.loadStatus`, whether
`postingInfo.isPosted` is on, and the rates to open and stop at — so there is no
second round trip while a carrier holds the line.

Because that endpoint serves *any* load regardless of posting, a payload with no
`postingInfo` block reads as **not on the board**. That is the safe direction
here, and the opposite of the Voice AI feed's provenance rule below.

`GET /voiceai/load/search_available` is still implemented and tested but is not on
the call path; it is the only endpoint returning `carrier_sales_data.book_now_url`.

---

## Field mapping

### Loads → `Load`

**Two payload shapes, one mapper.** The Voice AI feed and the load endpoints
(`/load/{id}`, `/load/search`) disagree about almost every field name, so each one
is read from whichever shape carries it rather than from an assumed layout.

| Domain field | Voice AI feed | Load endpoints |
|---|---|---|
| id | `load_id` | `id` |
| `status` | `load_status` | `status.loadStatus` |
| `is_posted` | *(which endpoint answered)* | `postingInfo.isPosted` |
| `open_rate` — **the floor the agent opens at** | `carrier_sales_data.load_board_rate` | `postingInfo.loadBoardRate` |
| `ceiling_rate` — **Max Buy, never exceeded** | `carrier_sales_data.max_buy` | `postingInfo.maxBuy` |
| stops | `shipment_information.waypoints` | top-level `waypoints` |
| stop type | `Pickup` / `Final Delivery` | `SH` / `CN` |
| stop city | flat on the stop | `location.city` |
| appointment | `appointment_date.start/end` | `appointmentTime.open/close` |
| `equipment`, `miles`, `commodity`, `pieces`, `weight` | `reference_information[]` pairs | `reference` **object** |
| `temperature` | — | `reference.reeferTemperature` |
| `notes` | `sales_notes.public_load_board_notes` | `postingInfo.comments` + each stop's `notes` |

`fraud_low_rate` is derived either way: `open_rate × TRANSPORT_PRO_FRAUD_LOW_RATIO`.

> **`billingInfo.charges.totalFreight` is never read as a rate.** That is what the
> customer pays us — $6,092 on a load with a $5,025 board rate — and quoting it to
> a carrier would hand over the entire margin. There is a test pinning this.

Three things about the load payloads that shape the mapping:

* **Stop notes are the requirements.** `postingInfo.comments` is usually null and
  the real instructions live on the stop — a BOL rule, a site's driver check-in
  procedure. They are labelled `At pickup:` / `At delivery:` because "bring your
  trailer plate" means something different at each end, and they are what routes
  the call through CHECK_REQUIREMENTS before any rate is discussed.
* **Notes contain markup and operator junk.** `<br/>` tags and rows of `####`
  typed in as dividers are invisible on screen and absurd read aloud, so they are
  stripped. A note that is *only* a reference number (`BOL # 0034850710`) is
  dropped entirely — it is not a requirement, and asking a driver whether they can
  comply with a BOL number is a strange turn.
* **`numberOfPieces: 0` and a lone `dimensions.length`** are both "nobody filled
  this in". Zero pieces is not spoken, and a length with no width or height is the
  trailer, not the freight — every 53-foot van has one.

#### Appointment times

Stops carry a real `ianaTimezone` and a numeric `timezone` offset, so times are
converted into the stop's own local time. IANA is preferred (hence the `tzdata`
dependency — Windows ships no zone database); the numeric offset is the fallback.

**A date marker is never converted.** When `open == close` the stamp is a date,
not a time — the load endpoints write one as local midnight. Converting it can
move the load a day: `2026-07-30T03:00:00Z` on a Chicago stop is July **29th** at
10 PM. So a marker's date is read exactly as written, and `appointmentStatus: "Not
Required"` is spoken as *"no appointment needed, first come first served"* rather
than as a clock time. Only a genuine two-ended window is converted and read as
one.

The rate mapping is the important row: `load_board_rate` / `max_buy` is exactly
the floor / ceiling split `NegotiationEngine` already worked in, so the agent
anchors at the board rate and never bids past Max Buy without a human.

A posted, open load with **no `load_board_rate`** is not quotable. There is no
honest number to open at and an invented anchor is one the desk may be held to,
so the agent hands that call to a rep. `lanevoice-tpcheck` flags it.

### Which loads get sold

Two conditions, and **both** must hold:

1. **status is `Ready To Dispatch`** — configurable via
   `TRANSPORT_PRO_OPEN_LOAD_STATUSES` (comma-separated; case and punctuation are
   normalised).
2. **posting is switched on** — `postingInfo.isPosted` is honoured strictly when
   the record carries it.

Both are checked **on the returned record**, always. `open_loads()` also sends
them to `GET /load/search` as `loadStatus` and `isPosted=true`, but a filter is a
request, not a guarantee — a search endpoint that doesn't recognise one tends to
ignore it rather than reject it. The record check is what actually holds.

**When a record carries no posting flag**, which endpoint answered decides:

| Source | No `postingInfo` means |
|---|---|
| `GET /load/{id}` | **not posted** — it serves any load, so silence is not evidence |
| `GET /load/search?isPosted=true` | posted — that is what was asked for |
| `/voiceai/load/search_available` | posted — it is literally *Search Posted Loads* |

> ⚠️ **The status vocabulary differs across endpoints.** The collection's
> `/voiceai/load/search_available` example answers with `"load_status":
> "AVAILABLE"`, while `/load/search` filters on `"ready to dispatch"`. Out of the
> box the agent sells **only** Ready To Dispatch, so if your live Voice AI
> endpoint speaks `AVAILABLE`, nothing will be offered until you add it. Run
> `lanevoice-tpcheck --load <id> --raw` — it prints the status it actually saw
> next to the accepted set — then set `TRANSPORT_PRO_OPEN_LOAD_STATUSES`. The
> repository also logs an explicit error when a whole board comes back unsellable.

Statuses that fail the check are not all the same to the caller:

| Status reads as | `LoadStatus` | What the carrier is told |
|---|---|---|
| in the accepted set | `OPEN` | the load |
| covered / booked / dispatched / in transit / delivered / billed | `COVERED` | "that one's already covered" |
| cancelled | `CANCELLED` | "that one isn't available to book right now" |
| anything else — available, planned, on hold, quoted, or a value we weren't told about | `NOT_READY` | "that one isn't available to book right now" |

`NOT_READY` exists to keep the agent honest. *"That load's already covered"* is a
specific claim about the freight, and a caller repeats it to the shipper — so it
is only made when the board actually says somebody has the load. A load that is
merely not released yet gets the true, vaguer sentence and no guess about why.

### Carriers — `carrier_status` → `Carrier`

The API collection has **no saved example** for `/voiceai/carrier_status`, so
`map_carrier` finds its fields by name across whatever shape arrives. Here is what
the live tenant actually returns:

```json
{
  "carrier_record": [
    { "id": 18885, "status": "FAIL", "carrier_name": "Creed Transport Inc",
      "city": "Burr Ridge", "state": "Illinois",
      "dot_number": "2999221", "mc_number": "23152" }
  ],
  "carrier_onboarding_team": { "contact": null, "email": null,
                               "phone": "260-208-4500" }
}
```

Two traps in that payload:

* The carrier is inside a **`carrier_record` list**, with `carrier_onboarding_team`
  sitting at the same depth. It is unwrapped explicitly, so the onboarding team's
  phone number can never be read as a carrier attribute.
* **`state` is "Illinois"** — the carrier's address, right beside `status`.
  Reading it as a status would suspend every carrier in Illinois, so `state` is
  deliberately excluded from the status field names.

**The vetting vocabulary is `ACTIVE` / `FAIL` / `REVIEW`** — nothing like the
active/inactive/suspended wording the rest of the API uses:

| Verdict | Result | What the carrier hears |
|---|---|---|
| `ACTIVE` | **PROCEED** | the load |
| `FAIL`, or anything unrecognised | **DECLINE** | "your company doesn't meet certain requirements to work with us" |
| `REVIEW` | **HUMAN_REVIEW** | a warm transfer to a rep |
| *no status field this code can find* | **HUMAN_REVIEW** | a warm transfer to a rep |

The bottom two rows are the ones worth defending. `REVIEW` means onboarding has
not finished — the carrier has failed nothing, and may well pass — so telling them
they don't meet our requirements is untrue and is the kind of thing a carrier
repeats to other brokers. The same goes for a status our own mapper couldn't find:
that is our bug, not their fault. Neither gets a load or a rate, but neither gets
accused either.

Unrecognised *values* still fail closed to suspended — `AuthorityStatus` never
guesses ACTIVE.

> The payload carries a `carrier_onboarding_team` phone. It is not used yet; it is
> the obvious thing to route a `REVIEW` carrier to if the desk wants that instead
> of the general rep queue.

**Insurance** is not enforced by default. The desk gate is authority, and
`carrier_status` may not report insurance at all — treating a missing field as
uninsured would decline every caller. Once you know the live payload carries it,
set `TRANSPORT_PRO_REQUIRE_INSURANCE_FIELD=1` and a missing one becomes a hard
stop.

---

## The three gates

```
IDENTIFY_LOAD    posted, open, and carrying a published rate
VERIFY_CARRIER   the MC/USDOT is in the system as ACTIVE
CONFIRM_EMAIL    the address is already on their account, AND the booking
                 lands in Transport Pro — before anyone hears "booked"
```

### The booking gate

The booking link goes to whatever address clears this gate, so an address **not
on the carrier's Transport Pro account does not clear it**. That rules out the
obvious attack on a voice desk: somebody who has learned a real MC number talking
us into mailing the booking link to an address they control.

* address is on the account → booked, link goes there
* they point at the account ("use the one you've got") → booked, most recent one
* anything else, after one more ask → **not booked**, warm transfer to a rep

The re-ask exists because the ordinary cause is a misheard domain, not fraud. The
agent never reads the real addresses out while querying one — that would hand an
impostor the answer.

Addresses are **never written back**: the Public API has no create-contact
endpoint, so `add_carrier_email` returns False and the agent never claims to have
saved one. New addresses are captured in the call note and in the posted offer,
which is where a rep will look.

### Booking is recorded before it is announced

`make_offer` is what "booked" means through this API — there is no separate book
endpoint. If that write fails, **nobody is told they're booked**; the call goes to
a rep with the rate and address already in the note. A carrier who hears "you're
booked" and has no load against their name shows up at a shipper for freight that
isn't theirs.

---

## Timestamps — the assumption to verify

Appointment times arrive stamped `Z` but behave like **local wall-clock time**.
The collection contains windows such as `11:00Z` to `05:00Z`: read as UTC that
window ends six hours before it starts; read as local times it is an ordinary
11 AM to 5 PM.

So dates and times are taken **exactly as written and never shifted**, and the
record's own `timezone` label is spoken alongside them ("7 AM to 2 PM CST"). When
a window still reads backwards, only the start is spoken, as an appointment — a
nonsense window is never read to a driver.

**Check one real load's pickup window against the Transport Pro UI before
go-live.** If the API is genuinely returning UTC, `_split_timestamp` in
`mappers.py` is where the conversion belongs.

---

## Known limitations

* **Partial MC/USDOT matching is gone.** The "we heard four of six digits, let me
  confirm you by company name" recovery needed a prefix search, and this API has
  none, so `carriers_matching_digits` returns nothing. The other recovery path
  still works: `digit_readings` proposes complete candidate numbers (for a caller
  who was cut off, or who started over) and each is looked up. A caller we only
  ever hear a fragment from is asked again, then handed to a rep.
* **`add_carrier_capacity` is implemented and tested but not wired into the call
  flow.** The API requires an email or phone plus a contact name, and at the
  point of the empty call the agent has none of them — it has the truck's
  location and the MC. Wiring it would mean asking a carrier for contact details
  before they have a reason to give them. `record_capacity` is ready on the
  repository when there is a point in the flow that has those fields.
* **Reps are local.** A rep here is a warm-transfer target (a name and a phone),
  not a Transport Pro record, so `available_rep` reads the seeded table.

---

## Caching and concurrency

One repository instance is shared by every call a worker process handles, so
reads are cached with an expiry rather than per call:

* loads — 60s (`TRANSPORT_PRO_LOAD_CACHE_SECONDS`); short, because a load can be
  covered by somebody else mid-call
* carriers and their contacts — 300s; vetting status does not move

Negative results are cached too, so a caller reading a wrong number back twice
doesn't cost two round trips. Token refresh is locked. Every read is one retry at
most — a carrier is holding the line, and a second and third round trip against a
struggling endpoint costs more in dead air than it can win.

An API failure raises `SourceUnavailable`, which the agent turns into a handoff.
It is never reported to the caller as "there's no such load" — that is a
statement of fact, and making it because an API timed out tells a carrier
something untrue about their freight.
