"""
Transport Pro JSON -> LaneVoice domain models.

Kept separate from the client so the translation is unit-testable against the
payloads in the API collection without touching HTTP, and so the awkward parts
of the wire format are documented in one place rather than spread through the
call flow.

Three properties of the feed shape everything here:

* **Fields are absent, null, or literally `false`.** The collection shows
  `"start": false` and `"timezone": false` where a value is missing, so a plain
  `is None` check is not enough — `_value` treats all three as "not given".

* **Rates come from `carrier_sales_data`.** `load_board_rate` is the number the
  desk anchors at (the agent's floor) and `max_buy` is the hard cap. That is
  exactly the `open_rate` / `ceiling_rate` split the negotiation engine already
  expects, so the mapping is direct. A load with no `load_board_rate` is not
  quotable and the agent hands it to a rep rather than inventing an anchor.

* **Timestamps are stamped `Z` but behave like local wall-clock time.** The
  collection contains appointment windows such as `11:00Z` to `05:00Z` — as UTC
  that window ends six hours before it starts, and as local times it is a
  perfectly ordinary 11 AM to 5 PM. So dates and times are read as wall clock
  and never shifted, and the record's own `timezone` label is spoken alongside
  them. When a window still reads incoherently (end before start) only the
  start is spoken, as an appointment — a nonsense window is never read to a
  carrier. Worth re-checking against live data before go-live.

Only carrier AUTHORITY drives the vetting gate, per the desk requirement: ACTIVE
proceeds, anything else does not. `carrier_status` has no saved example response
in the collection, so `map_carrier` searches for its fields by name across the
record instead of assuming one layout, and records the raw status string it read
so a status we cannot parse is told apart from one that says "inactive".
"""

from __future__ import annotations

import datetime
import html
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lanevoice.domain.models import AuthorityStatus, Carrier, Load, LoadStatus, Rep
from lanevoice.logging_config import get_logger

logger = get_logger(__name__)

# The only statuses the agent sells, unless `TRANSPORT_PRO_OPEN_LOAD_STATUSES`
# says otherwise. This is the desk requirement: Ready To Dispatch and nothing
# else. See `Settings.open_load_statuses` for why it is configurable.
_DEFAULT_OPEN_STATUSES = frozenset({"ready to dispatch"})

# Statuses that mean somebody else already has the freight. Told apart from the
# merely not-ready ones because "that load's already covered" is a specific claim,
# and making it about a load that is simply awaiting an appointment is false.
_COVERED_HINTS = ("cover", "booked", "dispatch", "transit", "deliver", "assigned",
                  "complete", "billed", "invoiced", "closed")

# Where a load carries its posting flag. `postingInfo.isPosted` is the shape the
# fuller load formats use; the rest are defensive.
_POSTED_KEYS = ("isposted", "is_posted", "posted", "postedtoloadboard")


def normalize_status(raw: Any) -> str:
    """Fold a status for comparison: `"Ready_To-Dispatch "` -> `"ready to dispatch"`."""
    text = str(raw or "").strip().lower().replace("-", " ").replace("_", " ")
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Field access
# --------------------------------------------------------------------------- #
def _value(raw: Any) -> Any:
    """The value, or None when the feed is saying "nothing here".

    Transport Pro spells a missing value three ways — absent, `null`, and
    `false` — and empty strings show up too. All four mean the same thing to us.
    """
    if raw is None or raw is False:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    return raw


def _text(raw: Any) -> str | None:
    """A speakable string, or None.

    Containers deliberately return None rather than their repr. `sales_notes`
    arrives as both a string and a `{"public_load_board_notes": ...}` object
    depending on the endpoint, and stringifying the object here put
    `{'public_load_board_notes': 'test board comment'}` into the load's spoken
    requirements — a dict read out loud to a driver.
    """
    if isinstance(raw, dict | list | tuple):
        return None
    value = _value(raw)
    return str(value).strip() or None if value is not None else None


def _number(raw: Any) -> float | None:
    """A rate or a count, however the feed typed it ("1,600" and 1600 both work)."""
    value = _value(raw)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _int(raw: Any) -> int | None:
    value = _number(raw)
    return int(value) if value is not None else None


def _norm(key: str) -> str:
    """Fold a field name so `mc_number`, `mcNumber` and `MC Number` all match."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _index(record: Any, depth: int = 3) -> dict[str, Any]:
    """Every scalar in a record, keyed by normalised field name, shallowest wins.

    Used only for `carrier_status`, whose layout the collection doesn't show.
    Shallow keys take precedence so a top-level `status` is never shadowed by
    one buried in a nested contact.
    """
    flat: dict[str, Any] = {}

    def walk(node: Any, level: int) -> None:
        if level > depth or not isinstance(node, dict):
            return
        for key, raw in node.items():
            if isinstance(raw, dict | list):
                continue
            flat.setdefault(_norm(key), raw)
        for raw in node.values():
            if isinstance(raw, dict):
                walk(raw, level + 1)
            elif isinstance(raw, list):
                for item in raw[:5]:
                    walk(item, level + 1)

    walk(record, 1)
    return flat


def _pick(flat: dict[str, Any], *names: str) -> Any:
    """First of `names` that the record actually carries a value for."""
    for name in names:
        value = _value(flat.get(_norm(name)))
        if value is not None:
            return value
    return None


_ABSENT = object()


def _pick_raw(flat: dict[str, Any], *names: str) -> Any:
    """First of `names` that is PRESENT, untouched — `False` included.

    `_value` folds `False` into "not given", which is right for a date or a rate
    and wrong for a flag: a record saying `is_active: false` is making a
    statement, and reading it as an absence turned a plainly inactive carrier
    into an unreadable one.
    """
    for name in names:
        if (key := _norm(name)) in flat:
            return flat[key]
    return _ABSENT


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def _parse_instant(raw: Any) -> datetime.datetime | None:
    """An ISO timestamp -> a datetime, timezone-aware when the string says so."""
    text = _text(raw)
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        pass
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text.strip())
    if not match:
        return None
    try:
        return datetime.datetime.fromisoformat(match.group(1))
    except ValueError:
        return None


def _stop_zone(location: Any) -> tuple[datetime.tzinfo | None, str | None]:
    """The stop's own timezone, and a label for it.

    Returns `(tzinfo, label)`. `tzinfo` is None when the feed gave us nothing we
    can convert with — an abbreviation like "CST" is a label, not a zone, and
    guessing which "CST" it means is how you tell a driver the wrong hour.

    `ianaTimezone` is preferred and needs the `tzdata` package on Windows, which
    is why it is a declared dependency. The numeric `timezone` offset is the
    fallback and is present alongside it in the load payloads, so a missing
    tzdata degrades to a fixed offset rather than to nothing. That is only wrong
    for a stop whose DST changes between now and the appointment.
    """
    if not isinstance(location, dict):
        return None, None

    name = _text(location.get("ianaTimezone")) or _text(location.get("iana_timezone"))
    if name:
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError):
            logger.debug("No tzdata for %r; falling back to the numeric offset. "
                         "Install the `tzdata` package for exact local times.", name)

    offset = location.get("timezone")
    if isinstance(offset, int | float) and not isinstance(offset, bool):
        if -14 <= offset <= 14:
            return datetime.timezone(datetime.timedelta(hours=offset)), None
    return None, _text(offset)      # e.g. the voiceai feed's "AST"


def _local(when: datetime.datetime | None,
           zone: datetime.tzinfo | None) -> datetime.datetime | None:
    """Express an instant in the stop's local time.

    A naive timestamp is already wall clock and is left alone. An aware one is
    converted when we have a real zone; when we don't, its own offset is dropped
    rather than applied, which keeps the historical behaviour for the Voice AI
    feed (see the module docstring) instead of shifting by a guess.
    """
    if when is None:
        return None
    if when.tzinfo is None:
        return when
    if zone is None:
        return when.replace(tzinfo=None)
    return when.astimezone(zone).replace(tzinfo=None)


def _spoken_time(when: datetime.time) -> str:
    """`time(17, 22)` -> `"5:22 PM"`; a whole hour drops the `":00"`."""
    hour = when.hour % 12 or 12
    meridiem = "AM" if when.hour < 12 else "PM"
    if when.minute:
        return f"{hour}:{when.minute:02d} {meridiem}"
    return f"{hour} {meridiem}"


# Appointment statuses meaning "turn up when you like".
_NO_APPOINTMENT = ("not required", "none", "fcfs", "first come", "flexible", "open")


def _appointment(waypoint: Any) -> tuple[str | None, str | None]:
    """A stop -> (ISO date, spoken window).

    Handles both shapes: `appointment_date.{start,end}` from the Voice AI feed and
    `appointmentTime.{open,close}` from the load endpoints. Times are converted
    into the stop's own local zone when the feed gives us one.

    A window is only spoken when it IS one. Three things are deliberately not:

    * **Local midnight.** The load endpoints store a date-with-no-appointment as
      midnight local (`2026-07-29T04:00:00Z` in New York). Reading that back as
      "12 AM" would send a driver to a dock at midnight.
    * **`appointmentStatus: "Not Required"`.** Said as first-come-first-served,
      which is what the carrier needs to know, rather than as a clock time.
    * **A pair that reads backwards.** The Voice AI examples contain windows
      whose end precedes their start; only the start is spoken.
    """
    if not isinstance(waypoint, dict):
        return None, None

    node = waypoint.get("appointmentTime")
    if not isinstance(node, dict):
        node = waypoint.get("appointment_date")
    if not isinstance(node, dict):
        return None, None

    zone, label = _stop_zone(waypoint.get("location"))
    if zone is None and label is None:
        # The Voice AI feed hangs its abbreviation off the appointment itself.
        label = _text(node.get("timezone"))

    raw_start = _parse_instant(node.get("start") or node.get("open"))
    raw_end = _parse_instant(node.get("end") or node.get("close"))
    start, end = _local(raw_start, zone), _local(raw_end, zone)
    if start is None and end is None:
        return None, None

    suffix = f" {label}" if label else ""
    midnight = datetime.time(0, 0)

    # A genuine window: two different times. Both are real instants, so both are
    # converted and the date comes from the converted start.
    if start and end and end > start:
        return start.date().isoformat(), (
            f"{_spoken_time(start.time())} to {_spoken_time(end.time())}{suffix}")

    # Everything below is a SINGLE stamp, and its time is only meaningful if the
    # stop actually holds an appointment. When it doesn't, the stamp is a date
    # marker — the load endpoints write one as midnight local, and `open` equals
    # `close`. A marker's date is read AS WRITTEN, never converted: shifting a
    # date-only value across zones is how a pickup lands on the wrong day.
    # (`2026-07-30T03:00:00Z` on a Chicago stop converts back to July 29th.)
    anchor_raw = raw_start or raw_end
    anchor = start or end
    written_date = anchor_raw.date().isoformat()

    status = normalize_status(node.get("appointmentStatus")
                              or waypoint.get("appointmentStatus"))
    if status and any(hint in status for hint in _NO_APPOINTMENT):
        return written_date, "no appointment needed, first come first served"
    if anchor.time() == midnight:
        return written_date, None
    if start is None:
        return anchor.date().isoformat(), f"by {_spoken_time(anchor.time())}{suffix}"
    return anchor.date().isoformat(), (
        f"{_spoken_time(anchor.time())} appointment{suffix}")


# --------------------------------------------------------------------------- #
# Loads
# --------------------------------------------------------------------------- #
def _place(waypoint: dict) -> str:
    """"Nashville, TN". The city sits flat on the stop or under `location`."""
    if not isinstance(waypoint, dict):
        return ""
    location = waypoint.get("location")
    source = location if isinstance(location, dict) else waypoint
    city = _text(source.get("city")) or _text(waypoint.get("city"))
    state = _text(source.get("state")) or _text(waypoint.get("state"))
    if city and city.isupper():
        # The load endpoints shout their cities ("SIKESTON"). Left as-is it reads
        # fine on screen and is a coin toss whether TTS spells it out.
        city = city.title()
    if city and state:
        return f"{city}, {state}"
    return city or state or ""


def _waypoints(*holders: Any) -> list[dict]:
    """The stops, from the first holder that has any.

    They live at the top level on the load endpoints and under
    `shipment_information` (or `dispatch_information`) on the Voice AI feed.
    """
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        raw = holder.get("waypoints")
        if isinstance(raw, list):
            stops = [w for w in raw if isinstance(w, dict)]
            if stops:
                return stops
    return []


# Stop type codes. `SH`/`CN` are shipper and consignee on the load endpoints; the
# Voice AI feed spells them out. Anything unrecognised falls back to position.
_PICKUP_TYPES = frozenset({"sh", "pu", "pickup", "shipper", "origin", "pick up"})
_DELIVERY_TYPES = frozenset({"cn", "do", "del", "dc", "delivery", "consignee",
                             "destination", "final delivery", "drop"})


def _is_pickup(waypoint: dict) -> bool:
    kind = normalize_status(waypoint.get("type"))
    return kind in _PICKUP_TYPES or "pickup" in kind or "pick up" in kind


def _is_delivery(waypoint: dict) -> bool:
    kind = normalize_status(waypoint.get("type"))
    return kind in _DELIVERY_TYPES or "deliver" in kind or "consignee" in kind


def _pickup_and_delivery(waypoints: list[dict]) -> tuple[dict | None, dict | None]:
    """The stop the carrier loads at, and the one they finish at.

    A load can have several stops; the carrier is being sold the ends of it. A
    stop explicitly marked `Final Delivery` wins outright, then the last delivery,
    then the last stop — a middle drop never becomes the destination.
    """
    pickups = [w for w in waypoints if _is_pickup(w)]
    finals = [w for w in waypoints if "final" in normalize_status(w.get("type"))]
    deliveries = [w for w in waypoints if _is_delivery(w)]

    pickup = pickups[0] if pickups else (waypoints[0] if waypoints else None)
    delivery = (finals[-1] if finals
                else deliveries[-1] if deliveries
                else waypoints[-1] if len(waypoints) > 1 else None)
    if delivery is pickup and len(waypoints) > 1:
        delivery = waypoints[-1]
    return pickup, delivery


def _references(record: dict, shipment: dict,
                waypoints: list[dict]) -> dict[str, Any]:
    """Everything the API calls a "reference", flattened into one lookup.

    Three shapes feed this, in increasing precedence:

    * the stops' own `reference` lists — `[{"type": "WEIGHT", "value": 38309}]`
    * the Voice AI feed's `shipment_information.reference_information` —
      `[{"key": "total_miles", "value": 1142}]`
    * the load endpoints' top-level `reference` OBJECT — `{"equipmentType":
      "Reefer", "miles": 958.58, ...}`

    The load-level object wins over a stop's, because a per-stop weight describes
    that stop while the load-level one describes the shipment.
    """
    flat: dict[str, Any] = {}

    def absorb(pairs: Any, key_field: str) -> None:
        if not isinstance(pairs, list):
            return
        for item in pairs:
            if isinstance(item, dict) and item.get(key_field):
                flat[_norm(item[key_field])] = item.get("value")

    for stop in waypoints:
        absorb(stop.get("reference"), "type")
    absorb(shipment.get("reference_information"), "key")
    absorb(record.get("reference_information"), "key")

    reference = record.get("reference")
    if isinstance(reference, dict):
        for key, raw in reference.items():
            if not isinstance(raw, dict | list):
                flat[_norm(key)] = raw
        dimensions = reference.get("dimensions")
        if isinstance(dimensions, dict):
            flat["dimensions"] = _spoken_dimensions(dimensions)
    return flat


def _spoken_dimensions(node: dict) -> str | None:
    """`{"length": "53.00", "width": null, "height": null}` -> None.

    A length on its own is the trailer, not the freight — every 53-foot van
    carries one, and announcing "dimensions: 53 feet" on every dry van is noise
    that sounds like a spec the carrier has to match. Two or more edges means
    somebody measured the cargo, and that is worth saying.
    """
    parts = []
    for axis, label in (("length", "long"), ("width", "wide"), ("height", "high")):
        value = _number(node.get(axis))
        if value:
            rendered = f"{value:g}"
            parts.append(f"{rendered} ft {label}")
    return " x ".join(parts) if len(parts) >= 2 else None


# `<br/>` is the API's line break, and runs of #, * or = are operator separators
# typed into a notes box. Both are silent on screen and absurd read aloud.
_HTML_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*(?:p|div|li|tr)\s*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")
_SEPARATOR_RUN_RE = re.compile(r"[#*=~_\-—–•·]{3,}")
# A note that is only a reference number is not a requirement, and asking a
# carrier "can you do that?" about "BOL # 0034850710" is a strange turn.
_REFERENCE_ONLY_RE = re.compile(
    r"^(?:bol|b/l|po|p\.o\.|pu|pickup|ref|reference|order|load|seal|appt)\s*"
    r"(?:number|no\.?|#)?\s*[:#\-]?\s*[\w\-/]+\.?$",
    re.IGNORECASE,
)


# A sentence whose content after the colon is an IDENTIFIER rather than prose:
# "PRO: 9003713875", "SCAC: CLNC", "EDISenderId: GPIAMERICASEZV", "Load Mode: TL".
# Detected by the absence of any ordinary lowercase word after a colon, which is
# what separates EDI plumbing from a real instruction like "Detention: paid at
# thirty an hour". Load 2513446's delivery stop is nine of these in a row, and read
# aloud they were 40% of everything the agent said about that load.
_PROSE_AFTER_COLON_RE = re.compile(r":.*\b[a-z]{3,}\b")


def _is_reference_metadata(sentence: str) -> bool:
    return ":" in sentence and not _PROSE_AFTER_COLON_RE.search(sentence)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|(?<=\.)(?=[A-Z])", text)
            if s.strip()]


def _tidy_note_body(text: str) -> str:
    """Drop reference metadata, then drop sentences we have already said.

    Both matter because the same instructions arrive from more than one place. The
    load's own notes and the shipper's Instructions/Directions field overlap almost
    entirely on the live tenant — "Must check in as Circle Logistics" appears in
    both, one with a full stop and one without — and every repeat is a sentence
    spoken twice down a phone.

    Comparison is on the folded text (lowercase, punctuation stripped) so a
    trailing full stop does not defeat it, which is exactly how the duplicate
    survived the block-level check in `_notes`.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for sentence in _sentences(text):
        if _is_reference_metadata(sentence):
            continue
        folded = " ".join(re.sub(r"[^a-z0-9 ]", " ", sentence.lower()).split())
        if not folded or folded in seen:
            continue
        seen.add(folded)
        kept.append(sentence.rstrip(". "))
    return ". ".join(kept)


def _clean_note(raw: Any) -> str | None:
    """A notes field -> something speakable, or None if nothing survives."""
    text = _text(raw)
    if not text:
        return None
    text = _HTML_BREAK_RE.sub(". ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _SEPARATOR_RUN_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Tidy the punctuation the substitutions above leave behind.
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"(?:\.\s*){2,}", ". ", text).strip(" .;:,")
    if not text or not re.search(r"[A-Za-z]", text):
        return None
    if _REFERENCE_ONLY_RE.match(text):
        return None
    # Deduplicate and strip reference plumbing SENTENCE by sentence. The check
    # above only catches a note that is nothing but a reference number; a stop
    # whose note is nine of them in a row got through it intact.
    text = _tidy_note_body(text)
    return text or None


def _load_status_text(record: dict) -> str:
    """The load's status, whichever of the API's spellings carries it."""
    for key in ("load_status", "loadStatus", "status"):
        raw = record.get(key)
        if isinstance(raw, dict):        # `{"status": {"loadStatus": "..."}}`
            for nested in ("load_status", "loadStatus", "status"):
                if (text := _text(raw.get(nested))):
                    return text
            continue
        if (text := _text(raw)):
            return text
    return ""


def _posting_flag(record: dict) -> bool | None:
    """Whether posting is switched on for this load. None if it doesn't say.

    `postingInfo.isPosted` is checked first because that is where the load
    formats put it. None is a real answer and distinct from False: a payload that
    carries no posting flag at all — which is the case for the collection's
    `search_available` example — must not be read as "not posted", or the entire
    posted board would be rejected. The caller falls back to which endpoint
    answered, which for `search_available` (a.k.a. "Search Posted Loads") is
    itself the posting filter.
    """
    holders = [record]
    for key in ("postingInfo", "posting_info", "posting"):
        nested = record.get(key)
        if isinstance(nested, dict):
            holders.insert(0, nested)     # nested wins over a top-level guess

    for holder in holders:
        for key, raw in holder.items():
            if _norm(key) not in _POSTED_KEYS:
                continue
            if isinstance(raw, bool):
                return raw
            if raw is None:
                continue
            text = str(raw).strip().lower()
            if text in ("true", "1", "yes", "y", "posted"):
                return True
            if text in ("false", "0", "no", "n", "unposted"):
                return False
    return None


def _notes(record: dict, shipment: dict, pickup: dict | None,
           delivery: dict | None) -> str | None:
    """Everything the carrier has to be told before they agree to the load.

    Three sources, and the stop notes are the ones that matter most in practice —
    that is where "CARRIER MUST SEND BOL PRIOR TO LEAVING THE SHIPPER" and a
    site's driver check-in procedure live. They are labelled by stop, because
    "bring your trailer plate" means something different at the shipper than at
    the consignee.

    Whatever comes back here gates the call: a load with notes goes through
    CHECK_REQUIREMENTS and the carrier is asked outright whether they can do it,
    before any rate is discussed. That is why `_clean_note` drops notes that are
    only a reference number — they are not requirements, and asking a driver
    whether they can comply with a BOL number is nonsense.
    """
    found: list[str] = []
    said: set[str] = set()

    def add(text: str | None) -> None:
        """Append a note, minus any sentence an earlier note already covered.

        Cross-source deduplication, which is where most of the length came from:
        the board comment, the load notes and the shipper's own instructions are
        largely the same sentences in a different order.
        """
        if not text:
            return
        fresh = []
        for sentence in _sentences(text):
            folded = " ".join(
                re.sub(r"[^a-z0-9 ]", " ", sentence.lower()).split())
            if not folded or folded in said:
                continue
            said.add(folded)
            fresh.append(sentence.rstrip(". "))
        if fresh:
            found.append(". ".join(fresh))

    # Board comments: the Voice AI feed's `sales_notes`, the load endpoints'
    # `postingInfo.comments`.
    for holder in (record.get("sales_notes"), shipment.get("sales_notes"),
                   record.get("carrier_sales_data"), record.get("postingInfo")):
        if isinstance(holder, dict):
            for key, raw in holder.items():
                if "note" in _norm(key) or "comment" in _norm(key):
                    add(_clean_note(raw))
        else:
            add(_clean_note(holder))
    for key in ("sales_notes", "load_board_notes", "public_load_board_notes",
                "notes", "comments"):
        add(_clean_note(record.get(key)))

    # Stop instructions, labelled so a driver knows where they apply.
    for stop, label in ((pickup, "At pickup"), (delivery, "At delivery")):
        if isinstance(stop, dict) and (text := _clean_note(stop.get("notes"))):
            add(f"{label}: {text}")

    return " ".join(found) or None


# Where each feed spells the carrier qualifications a load demands. The load
# endpoints use `reference.requiredClassifications`; the Voice AI feed uses
# `reference.required_carrier_classifications`.
_REQUIRED_CLASSIFICATION_KEYS = ("requiredclassifications",
                                 "requiredcarrierclassifications")


def _required_classifications(*holders: Any) -> tuple[str, ...]:
    """Qualifications the carrier must hold, from whichever feed answered.

    Deduplicated while keeping first-seen order: this tenant really does return
    `["Critical Cargo", "Critical Cargo"]` (load 2487956 among others), and a
    doubled entry would make any "how many requirements" check wrong.

    Read straight off the `reference` object rather than through `_references`,
    which drops list values on purpose — everything that goes through there can
    end up spoken, and a list of qualifications must never be read to a caller.
    """
    found: list[str] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        reference = holder.get("reference")
        if not isinstance(reference, dict):
            continue
        for key, raw in reference.items():
            if _norm(key) not in _REQUIRED_CLASSIFICATION_KEYS:
                continue
            # A single classification may arrive bare rather than in a list.
            values = raw if isinstance(raw, list | tuple) else [raw]
            for value in values:
                if (text := _text(value)) and text not in found:
                    found.append(text)
    return tuple(found)


def _commodity_value(*holders: Any) -> float | None:
    """The load's declared freight value, or None when it doesn't declare one.

    Zero is folded into None by `_number`'s callers here on purpose: a declared
    value of 0 is "nobody filled this in", and treating it as a real $0 would
    make every carrier's insurance limit look sufficient.
    """
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        reference = holder.get("reference")
        if not isinstance(reference, dict):
            continue
        for key, raw in reference.items():
            if _norm(key) == "commodityvalue" and (value := _number(raw)):
                return value
    return None


def map_load(record: dict, *, posted: bool, fraud_low_ratio: float = 0.5,
             open_statuses: frozenset[str] | None = None) -> Load | None:
    """One `search_available` / load-detail record -> a `Load`.

    Two conditions decide whether the agent may sell it, and both are checked on
    the record rather than assumed from the request:

    * **status** must be in `open_statuses` (default: Ready To Dispatch only).
      A search endpoint that doesn't recognise a filter tends to ignore it rather
      than reject it, so filtering server-side is a request, not a guarantee.
    * **posting** must be on. `postingInfo.isPosted` is honoured strictly when the
      record carries it. When it carries no posting flag at all, `posted` — which
      endpoint answered — stands in: `search_available` is "Search Posted Loads",
      so a record from it is posted by definition, and one found only via load
      detail is not on the board.

    Returns None only when the record has no load id at all. Everything else is
    mapped and then gated by `Load.is_bookable` / `Load.is_quotable`, so a load
    that fails a condition becomes something the agent declines to sell rather
    than something it never heard of.
    """
    load_id = _text(record.get("load_id")) or _text(record.get("id"))
    if not load_id:
        logger.warning("Transport Pro load record has no load_id: keys=%s",
                       sorted(record)[:12])
        return None

    shipment = record.get("shipment_information")
    shipment = shipment if isinstance(shipment, dict) else {}
    # Stops sit at the top level on the load endpoints and under
    # `shipment_information` on the Voice AI feed. `dispatch_information` is the
    # last resort: a dispatched load keeps its stops there.
    waypoints = _waypoints(record, shipment, record.get("dispatch_information"))
    pickup, delivery = _pickup_and_delivery(waypoints)
    references = _references(record, shipment, waypoints)

    pickup_date, pickup_window = _appointment(pickup)
    delivery_date, delivery_window = _appointment(delivery)

    # The desk's numbers. `carrier_sales_data.load_board_rate` / `max_buy` on the
    # Voice AI feed; `postingInfo.loadBoardRate` / `maxBuy` on the load endpoints.
    # Deliberately NOT `billingInfo.charges.totalFreight` — that is what the
    # customer pays us, and quoting it to a carrier would hand them the margin.
    rates = record.get("carrier_sales_data")
    if not isinstance(rates, dict) or not rates:
        nested = shipment.get("carrier_sales_data")
        rates = nested if isinstance(nested, dict) else {}
    posting = record.get("postingInfo")
    posting = posting if isinstance(posting, dict) else {}

    floor = (_number(rates.get("load_board_rate"))
             or _number(posting.get("loadBoardRate"))
             or _number(posting.get("load_board_rate")))
    max_buy = (_number(rates.get("max_buy"))
               or _number(posting.get("maxBuy"))
               or _number(posting.get("max_buy")))
    # A Max Buy with no Load Board Rate is NOT an anchor. Opening at the cap is
    # the one number the agent must never say first: the carrier accepts it, and
    # the desk has paid its own maximum with no room to have done better. It is
    # strictly worse than not quoting, so the load goes to a rep exactly like one
    # with no rates at all. (Observed live: a handful of loads on the board sit in
    # this state, one of them at $8300.)
    if floor is None and max_buy is not None:
        logger.warning(
            "Transport Pro load %s publishes a Max Buy of %s but no Load Board "
            "Rate. There is no anchor to open at — opening at the cap would spend "
            "the whole range on the first sentence — so it will go to a rep.",
            load_id, max_buy)
    open_rate = floor or 0.0
    # A cap below the anchor would let the engine build an inverted range.
    ceiling_rate = max(max_buy, open_rate) if max_buy is not None else open_rate

    # -- condition 1: status must be one we sell --------------------------- #
    allowed = _DEFAULT_OPEN_STATUSES if open_statuses is None else open_statuses
    status_text = normalize_status(_load_status_text(record))
    if status_text in allowed:
        status = LoadStatus.OPEN
    elif "cancel" in status_text:
        status = LoadStatus.CANCELLED
    elif any(hint in status_text for hint in _COVERED_HINTS):
        status = LoadStatus.COVERED
    else:
        # On the board but not in a sellable state — or a status vocabulary we
        # were not told about. Both end in "not something I can book for you",
        # and the log says which value it was so a mismatch is obvious.
        status = LoadStatus.NOT_READY
        logger.warning(
            "Transport Pro load %s has status %r, which is not one the agent "
            "sells (accepted: %s). It will not be offered. If this status IS "
            "sellable, add it to TRANSPORT_PRO_OPEN_LOAD_STATUSES.",
            load_id, status_text or "(none)", sorted(allowed) or "(none)")

    # -- condition 2: posting must be switched on -------------------------- #
    flag = _posting_flag(record)
    is_posted = posted if flag is None else flag
    if flag is False:
        logger.info("Transport Pro load %s has isPosted=false — not offered.",
                    load_id)

    def reference(*names: str) -> Any:
        for name in names:
            if (value := _value(references.get(_norm(name)))) is not None:
                return value
        return None

    return Load(
        load_id=load_id,
        origin=_place(pickup) if pickup else "",
        destination=_place(delivery) if delivery else "",
        pickup_date=pickup_date or "",
        equipment=_text(reference("required_trailer", "equipment_type",
                                  "trailer_type", "equipment"))
        or _text(record.get("equipment_type")) or "",
        weight_lbs=_int(reference("weight", "total_weight")) or 0,
        open_rate=open_rate,
        ceiling_rate=ceiling_rate,
        fraud_low_rate=open_rate * fraud_low_ratio,
        assigned_rep_id=_assigned_rep(record, shipment),
        status=status,
        is_posted=is_posted,
        notes=_notes(record, shipment, pickup, delivery),
        miles=_int(reference("total_miles", "miles")) or None,
        commodity=_text(reference("commodity")),
        # A piece count of zero is "nobody filled this in", not "zero pallets" —
        # and `facts()` would happily read "Pieces: 0" to a driver.
        pieces=_int(reference("pieces", "piece_count", "number_of_pieces")) or None,
        dimensions=_text(reference("dimensions")),
        temperature=_temperature(reference("reefer_temperature",
                                           "reefer_temperature_set_point",
                                           "temperature")),
        pickup_window=pickup_window,
        delivery_date=delivery_date,
        delivery_window=delivery_window,
        # Gate inputs, not spoken facts — see the fields' comments on `Load`.
        required_classifications=_required_classifications(record, shipment),
        commodity_value=_commodity_value(record, shipment),
        terminal_id=_terminal_id(record, shipment),
        origin_lat=_coordinate(pickup, "latitude"),
        origin_lon=_coordinate(pickup, "longitude"),
    )


def _coordinate(waypoint: Any, key: str) -> float | None:
    """`waypoints[n].location.latitude` — a real number, or None.

    Zero is rejected along with the usual absences. A stop at exactly 0°N 0°E is
    in the Atlantic off Ghana, so a zero here is a placeholder, and treating it as
    a position would put every such load a few thousand miles from the caller.
    """
    if not isinstance(waypoint, dict):
        return None
    location = waypoint.get("location")
    source = location if isinstance(location, dict) else waypoint
    value = _number(source.get(key))
    return value if value else None


# Who on a load a carrier call belongs to, in order. The carrier sales rep is
# whose freight this is from the carrier's side; the order taker is the fallback
# on a load that has no CSR yet. Billing and dispatch contacts are not people a
# carrier should be handed to.
_REP_CONTACT_TYPES = ("CARRIERSALESREP", "ORDERTAKER")


def _assigned_rep(*holders: Any) -> str | None:
    """The Transport Pro user who owns this load, as a string id, or None.

    `/load/{id}` lists the people on a load under `internalContacts` as
    `{type, id}` pairs — verified live on load 2551913:
    `[{"type": "ORDERTAKER", "id": 2237}, {"type": "CARRIERSALESREP", "id": 4507}]`.
    The id is what `TransportProClient.user` turns into a name and a number, and
    what a `reps.toml` entry is keyed on when the desk wants to override either.
    """
    for holder in holders:
        contacts = holder.get("internalContacts") if isinstance(holder, dict) else None
        if not isinstance(contacts, list):
            continue
        by_type: dict[str, str] = {}
        for contact in contacts:
            if not isinstance(contact, dict) or contact.get("id") in (None, "", 0):
                continue
            kind = re.sub(r"[\s_-]", "", str(contact.get("type") or "")).upper()
            by_type.setdefault(kind, str(contact["id"]).strip())
        for wanted in _REP_CONTACT_TYPES:
            if wanted in by_type:
                return by_type[wanted]
    return None


# Which of a user's numbers to dial, in order. FAX is never dialled. Transport
# Pro writes numbers the way people type them ("260-208-4500 ext 1310") and a
# SIP transfer wants E.164 with no extension.
_DIALABLE_PHONE_TYPES = ("CELL", "MOBILE", "OFFICE", "MAIN", "HOME")
_EXTENSION_RE = re.compile(r"\b(?:ext|extension|x)\.?\s*(\d+)\b", re.IGNORECASE)


def _e164(raw: Any) -> tuple[str, str | None]:
    """`('+12602084500', '1310')` from `'260-208-4500 ext 1310'`; `('', None)`
    when nothing diallable is in it. US/Canada only — that is the desk."""
    text = str(raw or "")
    match = _EXTENSION_RE.search(text)
    extension = match.group(1) if match else None
    digits = re.sub(r"\D", "", _EXTENSION_RE.sub("", text))
    if len(digits) == 10:
        return f"+1{digits}", extension
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}", extension
    return "", extension


def map_rep(record: dict | None) -> Rep | None:
    """A Transport Pro user record -> the rep a call can be handed to, or None.

    `phone` is the number a SIP transfer dials, E.164, preferring a cell over the
    office line. An extension cannot ride a transfer, so an office number that
    carries one ("260-208-4500 ext 1310" — the live shape for a rep whose desk has
    no direct number) dials the main line and is logged, because the fix is a
    `reps.toml` entry with that rep's direct number. `phone` is "" when the record
    has nothing diallable at all; the agent then names the rep and promises a
    callback rather than a transfer it cannot make.
    """
    if not isinstance(record, dict) or record.get("id") in (None, ""):
        return None
    name = " ".join(part for part in (_text(record.get("firstName")),
                                      _text(record.get("lastName"))) if part)
    if not name:
        return None
    numbers: dict[str, Any] = {}
    for entry in record.get("phoneNumbers") or []:
        if isinstance(entry, dict):
            numbers.setdefault(str(entry.get("type") or "").upper(), entry.get("value"))
    phone, extension = "", None
    for kind in _DIALABLE_PHONE_TYPES:
        if kind in numbers:
            phone, extension = _e164(numbers[kind])
            if phone:
                break
    rep_id = str(record["id"]).strip()
    if phone and extension:
        logger.info("Transport Pro rep %s (%s) is listed at %s ext %s — a main line. A "
                    "transfer dials the main line; add their direct number to reps.toml "
                    "under id %r to reach them straight.", name, rep_id, phone, extension,
                    rep_id)
    elif not phone:
        logger.info("Transport Pro rep %s (%s) has no diallable number on file — the "
                    "agent will promise a callback, not a transfer. Add one to "
                    "reps.toml under id %r.", name, rep_id, rep_id)
    return Rep(rep_id=rep_id, name=name, phone=phone, available=True)


def _terminal_id(*holders: Any) -> str | None:
    """Which terminal owns this load, as a string.

    `assignedTerminal` holds the terminal's integer **id** — 1003 is Fort Wayne
    Office, 1078 is one of its PODs — NOT its `terminalCode`. Confirmed against
    live data on both `/load/search` and `/load/{id}`.

    Normalised to a string because the terminal tree types the same identifier
    both ways (`{"id": 1058, "parentTerminalId": "1003"}`), and comparing an int
    against a str silently matches nothing.
    """
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        for key in ("assignedTerminal", "assigned_terminal", "terminalId"):
            if (value := _value(holder.get(key))) is not None:
                text = str(value).strip()
                if text and text != "0":
                    return text
        nested = holder.get("terminal")
        if isinstance(nested, dict) and (value := _value(nested.get("id"))):
            return str(value).strip()
    return None


def _temperature(raw: Any) -> str | None:
    """`"-20"` -> `"-20 F"`. A reefer setpoint the carrier has to be able to hold.

    Spoken because it is a condition of taking the load, not a detail: a driver
    who agrees to ice cream without hearing "minus twenty" has agreed to
    something else. Left alone if the feed already wrote a unit.
    """
    text = _text(raw)
    if text is None:
        return None
    if re.search(r"[a-zA-Z]", text):
        return text
    value = _number(text)
    return f"{value:g} F" if value is not None else text


# --------------------------------------------------------------------------- #
# Carriers
# --------------------------------------------------------------------------- #
# Field names that could carry the vetting status, most specific first. `state`
# is deliberately absent: it is the carrier's ADDRESS in this API, and reading it
# as a status would turn every Tennessee carrier into a suspended one.
_STATUS_KEYS = (
    "carrier_status", "broker_carrier_status", "carrierstatuscode",
    "authority_status", "operating_status", "approval_status",
    "compliance_status", "status_description", "status",
)
_NAME_KEYS = ("company_name", "carrier_name", "legal_name", "dba_name", "name")
_MC_KEYS = ("mc_number", "mc", "docket_number")
_DOT_KEYS = ("dot_number", "usdot_number", "us_dot_number", "dot")
_ID_KEYS = ("carrier_id", "broker_carrier_id", "brokercarrierrecordid", "id")

# Statuses that are real answers but not "active" — kept apart from a status we
# could not find at all, which is a mapping problem and goes to a human.
_INACTIVE_HINTS = ("inactive", "suspend", "revoked", "expired", "out of service",
                   "do not use", "donotuse", "blocked", "terminated", "pending",
                   "denied", "rejected", "declined", "not approved")


def _authority(flat: dict[str, Any]) -> tuple[AuthorityStatus, str | None]:
    """Read the vetting status, and hand back the raw string we read it from.

    The raw value is what lets the verification service tell "the feed says
    inactive" (decline the carrier) apart from "we could not find a status at
    all" (a human looks at it). Unrecognised values fail closed via
    `AuthorityStatus`, which never guesses ACTIVE.
    """
    raw = _pick(flat, *_STATUS_KEYS)
    if raw is None:
        # Some feeds carry a flag instead of a word. Read it raw: `false` here is
        # an answer, not a missing field.
        flag = _pick_raw(flat, "is_active", "active", "is_approved", "approved")
        if isinstance(flag, bool):
            return (AuthorityStatus.ACTIVE if flag else AuthorityStatus.INACTIVE,
                    str(flag))
        return AuthorityStatus.SUSPENDED, None
    text = str(raw).strip()
    if isinstance(raw, bool):
        return (AuthorityStatus.ACTIVE if raw else AuthorityStatus.INACTIVE), text
    return AuthorityStatus(text), text


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _emails(record: Any) -> tuple[str, ...]:
    """Every address anywhere in a record, in order, lowercased, deduplicated.

    Matched by SHAPE, not by field name. The live `/contact/search` returns them
    as `emailContacts: [{"type": "MAIN", "value": "..."}]` — a list under an
    email-ish key, holding objects whose address sits under a neutral `value`.
    Keying on the field name matched `emailContacts`, found a list where it wanted
    a string, and stopped: the booking gate then saw no addresses for any live
    carrier, and no call could ever reach a confirmed booking. Any `@`-shaped
    string in a contact record is an address, so that is what this looks for —
    and it survives whatever the API renames the field to next.
    """
    found: list[str] = []

    def walk(node: Any, level: int = 0) -> None:
        if level > 6:
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value, level + 1)
        elif isinstance(node, list | tuple):
            for item in node[:200]:
                walk(item, level + 1)
        elif isinstance(node, str):
            for match in _EMAIL_RE.findall(node):
                address = match.strip().lower()
                if address not in found:
                    found.append(address)

    walk(record)
    return tuple(found)


def map_carrier(record: dict, *, emails: tuple[str, ...] = (),
                insurance_reported_only: bool = False) -> Carrier | None:
    """A `carrier_status` record -> a `Carrier`.

    `insurance_reported_only` False (the default) means a record that says
    nothing about insurance is treated as insured, because the desk gate the
    agent enforces is AUTHORITY. Turn it on once the live payload is known to
    carry an insurance field, and a missing one becomes a hard stop.
    """
    flat = _index(record)
    name = _text(_pick(flat, *_NAME_KEYS))
    dot = _text(_pick(flat, *_DOT_KEYS))
    mc = _text(_pick(flat, *_MC_KEYS))
    if not (dot or mc):
        logger.warning("Transport Pro carrier record has no MC or DOT number: "
                       "keys=%s", sorted(flat)[:15])
        return None

    status, raw_status = _authority(flat)
    carrier_id = _text(_pick(flat, *_ID_KEYS))

    insured = _pick(flat, "insurance_on_file", "has_insurance", "insured",
                    "insurance_valid")
    if isinstance(insured, bool):
        insurance_on_file = insured
    elif insured is not None:
        insurance_on_file = str(insured).strip().lower() not in (
            "false", "no", "0", "expired", "lapsed", "none")
    else:
        insurance_on_file = not insurance_reported_only

    return Carrier(
        # USDOT is the primary key everywhere downstream (call records, the email
        # file), so a carrier that only gave an MC is keyed on MC rather than on
        # an empty string that would collide with every other such carrier.
        usdot_number=dot or f"MC{re.sub(r'[^0-9]', '', mc or '')}",
        mc_number=mc,
        legal_name=name or "this carrier",
        authority_status=status,
        insurance_on_file=insurance_on_file,
        approved=True,   # `carrier_status` answering at all means they're on file
        contact_emails=emails or _emails(record),
        carrier_id=carrier_id,
        raw_authority_status=raw_status,
    )


def contact_emails(records: list[dict]) -> tuple[str, ...]:
    """`/contact/search` results -> the addresses on the carrier's file."""
    found: list[str] = []
    for record in records:
        for address in _emails(record):
            if address not in found:
                found.append(address)
    return tuple(found)
