"""
How far is their truck from the pickup?

A carrier says "empty in Fort Wayne today". The load's pickup already carries
coordinates — Transport Pro puts `latitude`/`longitude` on every waypoint — so the
only missing piece is turning the spoken place into a point, and that is what this
module does.

Three deliberate limits, because this number gets SAID to a driver:

* **Straight-line, scaled.** Great-circle distance times a road factor
  (`DEADHEAD_ROAD_FACTOR`). Real driving miles run 15–25% over the straight line;
  the factor closes most of that gap but not all of it. This is a number to talk
  with, not a number to price with — if deadhead ever feeds a rate, it needs real
  road miles from a routing engine.

* **Spoken rounded, always.** `spoken_miles` rounds hard and the agent says
  "about 90 miles". Saying "97 miles" claims a precision this cannot support, and
  a driver who hears 97 and drives 130 has been misled by us.

* **City-level or nothing.** A caller who names only a state gets NO distance.
  A state centroid could be 200 miles from where the truck actually is, and a
  confidently wrong number is worse here than no number — the agent simply
  doesn't mention it.

* **The pickup steers the match.** Names repeat across the country and the
  recogniser mangles them. When the pickup's coordinates are known, a name
  shared by several towns resolves to the one nearest the pickup, and a name
  that only matches loosely is matched against the towns within `_REGION_MILES`
  of it before the whole country — a truck calling a Fort Wayne desk about a
  Fort Wayne load is far likelier to be in Columbia City, Indiana than Columbia
  City, Washington, whatever the population figures say.

The city table is bundled (`data/us_cities.csv.gz`, ~280 KB, every US place of
1,000+ people, ~17,000 of them) so this costs no network call on the critical
path and the test suite stays hermetic. A hamlet below that floor falls through
to None, which is the honest answer rather than a guess at the nearest town.

Data source: GeoNames (https://www.geonames.org/), CC BY 4.0, extracted via the
`geonamescache` package. See `data/SOURCE.md`.
"""

from __future__ import annotations

import csv
import difflib
import gzip
import io
import math
import re
import threading
from dataclasses import dataclass
from importlib import resources

from lanevoice.logging_config import get_logger
from lanevoice.parsing import STATE_CODES

logger = get_logger(__name__)

_DATA_FILE = "data/us_cities.csv.gz"

# Driving distance over great-circle distance. 1.2 is the usual rule of thumb for
# the US highway network; it is low for mountain west, high for the plains.
DEADHEAD_ROAD_FACTOR = 1.2

# Below this, "how far out are you" isn't a real question — they're basically there.
_TRIVIAL_MILES = 15

# How close a fuzzy match has to be. Deliberately tight: a loose match sends the
# agent confidently talking about the wrong town, which is worse than saying
# nothing. 0.86 accepts "ft wayne"/"fort wayne" and rejects "wayne"/"fort payne".
_FUZZY_CUTOFF = 0.86
# Towns within this many miles of the pickup are tried first for a loose match
# and win outright when several towns share the name the caller said.
_REGION_MILES = 500

# Speech-to-text and truckers both abbreviate. Applied to BOTH sides of the match
# so "St. Louis" in the table and "st louis" from the caller meet in the middle.
_EXPANSIONS = (("ft", "fort"), ("st", "saint"), ("mt", "mount"), ("pt", "port"))

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class Place:
    name: str
    state: str          # two-letter code
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


class _Cities:
    """The bundled table, parsed once on first use.

    Lazy because the offline demo and most of the test suite never geocode
    anything, and because paying 56 KB of gunzip at import time would slow every
    `lanevoice-*` entry point for a feature that only one call path uses.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_state: dict[str, list[Place]] = {}
        # Kept in FILE order, which is population-descending. Bucketing by state
        # loses that ordering, and it is the only thing that resolves a bare
        # "Springfield" to Missouri rather than to whichever state sorted first.
        self._all: list[Place] = []
        self._loaded = False

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True      # set first: a failed read must not retry per call
            try:
                blob = (resources.files("lanevoice")
                        .joinpath(_DATA_FILE).read_bytes())
                text = gzip.decompress(blob).decode("utf-8")
            except (OSError, FileNotFoundError, ValueError) as exc:
                logger.error(
                    "Could not read the bundled city table (%s): %s. Deadhead "
                    "distance will not be mentioned on calls.", _DATA_FILE, exc)
                return
            # Rows arrive largest-population first, so the first exact match for a
            # bare "Springfield" is the one a trucker means.
            for row in csv.DictReader(io.StringIO(text)):
                try:
                    place = Place(row["name"], row["state"],
                                  float(row["lat"]), float(row["lon"]))
                except (KeyError, TypeError, ValueError):
                    continue
                self._by_state.setdefault(place.state, []).append(place)
                self._all.append(place)
            logger.debug("City table loaded: %d places across %d states.",
                         len(self._all), len(self._by_state))

    def in_state(self, state: str) -> list[Place]:
        self._load()
        return self._by_state.get(state, [])

    def everywhere(self) -> list[Place]:
        self._load()
        # Population-descending, so a caller who names a city with no state gets
        # the one they almost certainly mean: "Springfield" is Missouri, not the
        # seven smaller ones.
        return self._all

    @property
    def loaded_count(self) -> int:
        self._load()
        return len(self._all)


_CITIES = _Cities()


def _normalise(name: str) -> str:
    """Fold a place name so a caller's spelling and the table's can meet."""
    text = _PUNCT_RE.sub(" ", str(name or "").lower())
    tokens = text.split()
    return " ".join(
        next((full for short, full in _EXPANSIONS if token == short), token)
        for token in tokens
    )


def _split(place: str) -> tuple[str, str | None]:
    """`"Fort Wayne, Indiana"` -> `("Fort Wayne", "IN")`.

    Handles the three shapes `parsing.extract_empty_location` produces: a
    "City, State" pair, a bare city, and a bare state. The state half arrives
    either as a full title-cased name or as a two-letter code.
    """
    text = " ".join(str(place or "").split())
    if not text:
        return "", None

    head, _, tail = text.rpartition(",")
    if head:
        return head.strip(), _state_code(tail)

    # No comma. It may be a bare state, which means no city precision at all.
    if _state_code(text):
        return "", _state_code(text)

    # Or a city with the state run onto it — "Phoenix Arizona". The parser emits a
    # comma today, but this arrives from a caller's own words often enough that
    # relying on that would drop a placeable location for want of punctuation.
    # Longest state name first, so "West Virginia" is not read as "Virginia".
    lowered = text.lower()
    for name in sorted(STATE_CODES, key=len, reverse=True):
        if lowered.endswith(f" {name}"):
            return text[: -len(name)].strip(), STATE_CODES[name]
    return text, None


def _state_code(raw: str) -> str | None:
    text = " ".join(str(raw or "").strip().split())
    if len(text) == 2 and text.isalpha():
        code = text.upper()
        return code if code in set(STATE_CODES.values()) else None
    return STATE_CODES.get(text.lower())


def state_only(place: str | None) -> str | None:
    """The state's code when `place` names a state and nothing else — "Indiana",
    "IN" — or None. Observed live: the recogniser dropped "Fort Wayne" off the
    front of "Fort Wayne, Indiana at 10 am" and the agent moved on with a state
    for a location; a city has to be asked for."""
    city, code = _split(place or "")
    return code if (code and not city) else None


def _miles_from(place: Place, near: tuple[float, float]) -> float:
    return haversine_miles(place.lat, place.lon, near[0], near[1])


def _nearest(places: list[Place], near: tuple[float, float]) -> Place:
    return min(places, key=lambda p: _miles_from(p, near))


def locate(place: str | None, near: tuple[float, float] | None = None) -> Place | None:
    """A spoken place -> a point, or None when we cannot place it confidently.

    `near` is the pickup, when its coordinates are known. It never widens what
    matches — a name that matches nothing still matches nothing — it decides
    BETWEEN matches: the nearest of several towns sharing a name, and the towns
    around the pickup before the rest of the country for a loose match.

    None is a first-class answer and the caller must treat it as "say nothing
    about distance". It happens for a state with no city, a hamlet below the
    table's floor, and a name mangled beyond a tight fuzzy match — and in every
    one of those cases a number would be a guess.
    """
    city, state = _split(place or "")
    if not city:
        if state:
            logger.debug("%r is a state with no city — no distance will be given.",
                         place)
        return None
    wanted = _normalise(city)
    if not wanted:
        return None
    pool = _CITIES.in_state(state) if state else _CITIES.everywhere()
    if not pool:
        if state:
            logger.debug("No cities on file for state %s.", state)
        return None

    exact = [p for p in pool if _normalise(p.name) == wanted]
    if exact:
        if len(exact) == 1 or near is None:
            return exact[0]                  # unique — or the most populous, the table's order
        best = _nearest(exact, near)
        logger.info("Heard %r; %d places share that name — taking %s, the nearest to "
                    "the pickup.", place, len(exact), best.label)
        return best

    tiers: list[list[Place]] = []
    if near is not None:
        tiers.append([p for p in pool if _miles_from(p, near) <= _REGION_MILES])
    tiers.append(pool)
    for candidates in tiers:
        index: dict[str, list[Place]] = {}
        for candidate in candidates:
            index.setdefault(_normalise(candidate.name), []).append(candidate)
        close = difflib.get_close_matches(wanted, list(index), n=1, cutoff=_FUZZY_CUTOFF)
        if close:
            options = index[close[0]]
            match = _nearest(options, near) if (near is not None and len(options) > 1) \
                else options[0]
            logger.info("Heard %r; matched it to %s.", place, match.label)
            return match
    logger.info("Could not place %r%s — no distance will be given.",
                place, f" in {state}" if state else "")
    return None


def region_keyterms(office: str | None, miles: float = 150.0, limit: int = 60,
                    local_miles: float = 30.0) -> list[str]:
    """The names of the towns around the office, for the recogniser's vocabulary.

    A caller's "Columbia City" or "Auburn" is a word the recogniser has to pick
    out of a phone line; giving it the towns a truck near this desk is likely to
    be empty in makes those the words it expects. Two tiers: every place within
    `local_miles` (the desk's own back yard, small towns included), then the
    largest places out to `miles`, up to `limit` names in all. Empty when the
    office cannot be placed.
    """
    centre = locate(office)
    if centre is None or limit <= 0:
        return []
    here = (centre.lat, centre.lon)
    everywhere = _CITIES.everywhere()          # population order
    names: list[str] = []
    for tier in (local_miles, miles):
        for place in everywhere:
            if _miles_from(place, here) <= tier and place.name not in names:
                names.append(place.name)
                if len(names) >= limit:
                    return names
    return names


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    radius = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(min(1.0, a)))


def deadhead_miles(place: Place, lat: float | None, lon: float | None, *,
                   road_factor: float = DEADHEAD_ROAD_FACTOR) -> float | None:
    """Estimated DRIVING miles from `place` to a pickup, or None.

    None whenever the pickup has no coordinates — plenty of real load records
    carry a city with null lat/lon, and inventing a distance for those is exactly
    what this returns None to avoid.
    """
    if lat is None or lon is None:
        return None
    try:
        straight = haversine_miles(place.lat, place.lon, float(lat), float(lon))
    except (TypeError, ValueError):
        return None
    return straight * max(1.0, road_factor)


def spoken_miles(miles: float | None) -> str | None:
    """The distance as a rep would say it, or None if it isn't worth saying.

    Rounded hard and deliberately: the underlying estimate is worth ±15%, so
    "about 90 miles" is honest where "97 miles" is not. Anything under
    `_TRIVIAL_MILES` reads as "right there" rather than a figure, because
    "about 10 miles" invites a precision argument over nothing.
    """
    if miles is None or miles < 0:
        return None
    if miles < _TRIVIAL_MILES:
        return "right around the corner from the pickup"
    if miles < 100:
        step = 10
    elif miles < 300:
        step = 25
    else:
        step = 50
    return f"about {int(round(miles / step) * step)} miles"


def deadhead_phrase(place: str | None, lat: float | None,
                    lon: float | None, *,
                    road_factor: float = DEADHEAD_ROAD_FACTOR) -> str | None:
    """The whole thing: spoken place + pickup point -> a speakable phrase or None.

    One entry point so the caller has exactly one thing to check. Every failure
    along the way — unplaceable city, state only, pickup with no coordinates —
    collapses to None, which means the agent says nothing about distance at all.
    """
    near = None
    if lat is not None and lon is not None:
        try:
            near = (float(lat), float(lon))
        except (TypeError, ValueError):
            near = None
    located = locate(place, near=near)
    if located is None:
        return None
    return spoken_miles(deadhead_miles(located, lat, lon,
                                       road_factor=road_factor))
