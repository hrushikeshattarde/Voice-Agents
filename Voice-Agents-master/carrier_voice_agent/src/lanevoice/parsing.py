"""
Utterance parsing — extract structured entities (load IDs, MC/DOT numbers,
dollar amounts) from a carrier's spoken text. Regex-first: cheap and reliable,
and unit-testable in isolation.
"""

from __future__ import annotations

import re

# Digits a caller spoke as words. "Oh" for zero is near-universal on the phone.
_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "nought": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_SPOKEN_DIGIT_RE = re.compile(
    r"\b(" + "|".join(_SPOKEN_DIGITS) + r")\b", re.IGNORECASE)

# A run of SINGLE digits separated by punctuation or spaces. Each element must be
# a lone digit, which is what keeps "42,000" and "$2,150" out of this.
_DIGIT_RUN_RE = re.compile(r"\b\d(?:[\s,.–—-]+\d\b)+")


def glue_spoken_digits(text: str) -> str:
    """Join up digits a caller read out one at a time.

    We ask them to say an MC number "slowly, one digit at a time" — and then STT
    hands back "6, 5, 4, 3, 2, 1", which no `\\d{4,8}` pattern will ever match. So
    the agent asks again, gets the same thing, and the caller hangs up on us.
    Spoken words are converted first ("six five four" -> "6 5 4").

    Only runs of single separated digits are joined, so quantities keep their
    shape: "42,000 lbs" and "$2,150" come through untouched.
    """
    converted = _SPOKEN_DIGIT_RE.sub(
        lambda m: _SPOKEN_DIGITS[m.group(1).lower()], text)
    return _DIGIT_RUN_RE.sub(lambda m: re.sub(r"\D", "", m.group()), converted)


def extract_load_id(text: str, *, numeric: bool = False) -> str | None:
    """The load number in what the caller just said, or None.

    Two formats, because it depends where the loads come from (see
    `Settings.numeric_load_ids`):

    * `numeric=False` — the seed data's `L1001`. Matches 'L1001', 'load 1001',
      'L 10 01' and 'L one zero zero one', and always returns it L-prefixed.
    * `numeric=True` — Transport Pro's bare ids, which run six and seven digits
      ('1303369', '2333606'). Returned exactly as heard, since that is what the
      API is keyed on.

    The numeric form needs five digits minimum. Rates are three and four digits
    and get said constantly on these calls, so a shorter run is far likelier to
    be money than a load number — and 'L1001' still needs only four, because the
    letter is doing the disambiguating there.
    """
    compact = glue_spoken_digits(text).upper().replace(" ", "")
    if numeric:
        match = re.search(r"\d{5,9}", compact)
        return match.group() if match else None
    match = re.search(r"L?\d{4,6}", compact)
    if not match:
        return None
    token = match.group()
    digits = re.sub(r"\D", "", token)
    return token if token.startswith("L") else f"L{digits}"


def heard_digits(text: str) -> str:
    """Every digit in an utterance, in order, with everything else dropped.

    Deliberately blunt: at the point we're trying to hear an identifier, "it's
    six five four" and "654" and "6-5-4" are the same thing, and we would rather
    hold three digits we can build on than nothing at all.
    """
    return re.sub(r"\D", "", glue_spoken_digits(text))


def digit_readings(held: str, heard: str) -> list[str]:
    """How the digits we just heard could relate to the ones we already had.

    A caller who gets cut off mid-number carries on from where they stopped; one
    who thinks we missed it starts over; one who is being careful backs up a
    couple of digits and then continues. All three are ordinary, and from the
    text alone they are indistinguishable — so we return every reading, longest
    first, and let the caller's own carrier file decide which one is real.

    Returned in the order worth trying, without duplicates.
    """
    readings: list[str] = []

    def add(value: str) -> None:
        if value and value not in readings:
            readings.append(value)

    if held and heard:
        # They repeated the tail before carrying on: "six five four" ... "five
        # four three two one" -> 654321, not 654654321. Longest overlap first.
        for k in range(min(len(held), len(heard)), 0, -1):
            if held.endswith(heard[:k]):
                add(held + heard[k:])
        add(held + heard)          # straight continuation
    add(heard)                     # they started the number over
    add(held)                      # nothing new was audible
    return readings


def extract_mc_dot(text: str) -> tuple[str | None, str | None]:
    """
    Return ('MC'|'DOT', number) or (None, None).

    Classifies by the label nearest the number, tolerating filler words
    ("my MC is 123456", "MC number 123456") and glued forms ("MC123456").
    Digits read out one at a time are joined first, since that is exactly how we
    ask for them. Defaults to DOT (the primary identifier per PRD §8.3) when
    unlabeled.
    """
    upper = glue_spoken_digits(text).upper()
    num = re.search(r"\d{4,8}", upper)
    if not num:
        return None, None
    number = num.group()

    window = upper[max(0, num.start() - 25):num.start()]
    _dot = r"US ?DOT|\bDOT\b"
    if re.search(r"\bMC\b", window) and not re.search(_dot, window):
        return "MC", number
    if re.search(_dot, window):
        return "DOT", number
    # No label next to the number — fall back to the whole utterance.
    if re.search(r"\bMC\b", upper) and not re.search(_dot, upper):
        return "MC", number
    return "DOT", number


# Words Whisper hallucinates on silence/noise (esp. "Thank you.", "you", "so").
# A turn made up ENTIRELY of these, with no digits, is almost certainly a phantom.
_NOISE_WORDS = {
    "thank", "thanks", "you", "bye", "goodbye", "so", "please", "subscribe",
    "for", "watching", "um", "uh", "ah", "mm", "hmm", "oh", "the", "a", "well",
}


def is_probably_noise(text: str) -> bool:
    """True if the transcript looks like a Whisper silence-hallucination."""
    if re.search(r"\d", text):          # any digit -> real content (load/MC/rate)
        return False
    words = re.findall(r"[a-z]+", text.lower())
    return not words or all(w in _NOISE_WORDS for w in words)


_TLDS = "com|net|org|io|co|us|biz|info|trucking|transport"


def _spoken_email(text: str) -> str | None:
    """Recover an address a human SAID rather than typed.

    Speech-to-text hands us "dispatch at blueskylogistics dot com" — or worse,
    "dispatch at blue sky logistics dot com" with the domain split into words.
    Only attempted when both an 'at' and a 'dot' are present, so ordinary
    sentences don't get mangled into addresses.
    """
    low = f" {text.lower().strip()} "
    if not re.search(r"\bdot\b", low) or not re.search(r"\bat\b", low):
        return None
    low = re.sub(r"\b(?:underscore|under score)\b", "_", low)
    low = re.sub(r"\b(?:dash|hyphen|minus)\b", "-", low)
    low = re.sub(r"\b(?:dot|period|point)\b", ".", low)
    low = re.sub(r"\bat\b", "@", low)
    low = re.sub(r"\s*([@._-])\s*", r"\1", low)       # tighten around separators
    # The domain may still be spoken as separate words — allow spaces up to the TLD.
    match = re.search(rf"([\w.+-]+)@([\w\s-]+?\.(?:{_TLDS}))\b", low)
    if not match:
        return None
    return f"{match.group(1)}@{match.group(2)}".replace(" ", "")


def extract_email(text: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if match:
        return match.group().rstrip(".")
    return _spoken_email(text)


def extract_phone(text: str) -> str | None:
    """A 10-digit US phone (kept separate from 6-digit MC / 4-digit rates)."""
    match = re.search(
        r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text
    )
    return match.group().strip() if match else None


# --------------------------------------------------------------------------- #
# The empty call: "when and where is your truck getting empty?"
#
# Answers come in every shape — "empty in Towson, Arizona", "right now, Dallas",
# "I deliver tomorrow morning in Laredo", or just half of it. We parse the two
# halves independently so the agent can follow up on whichever one is missing
# instead of re-asking the whole question.
# --------------------------------------------------------------------------- #
_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    "louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    "virginia|washington|west virginia|wisconsin|wyoming|district of columbia"
)
# Two-letter forms only count after a comma ("Dallas, TX") — bare "in", "or",
# "me" and "la" are ordinary English words and would match everything.
_STATE_ABBREVS = (
    "al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|"
    "mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|"
    "wi|wy|dc"
)

_WHEN_RE = re.compile(
    r"\b(?:"
    r"right now|as of now|already (?:empty|unloaded|off)|empty now|ready to (?:go|roll)|"
    r"rolling now|now|today|tonight|this (?:morning|afternoon|evening|afternoon)|"
    r"tomorrow(?: morning| afternoon| evening| night)?|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day(?: morning| afternoon| night)?|"
    r"in (?:an hour|a couple hours|a few hours|the morning|the afternoon|the evening)|"
    r"(?:couple|few) (?:of )?hours|"
    r"next (?:day|week|mon|tues|wednes|thurs|fri|satur|sun)(?:day)?|"
    r"(?:by|around|at|after|before)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|o'?clock)?|"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)"
    r")\b"
)

# A city is one to three words. Everything else in the utterance is lead-in
# ("I'm empty in…"), a time we've already pulled out, or filler — stripped off
# the ends by token, which is more reliable than trying to write one regex that
# only ever grabs the place.
_CITY = r"([a-z][a-z.'\-]*(?:\s+[a-z][a-z.'\-]*){0,2})"
_PLACE_FILLER = frozenset("""
empty empties emptying sitting sits unloading unload unloaded delivering deliver
delivers delivered loaded loading available free open be being is am are was were
i im we it its truck trucks the a an my our this that at in on near around out of
outside from to and then so just about like currently up right now today tomorrow
tonight morning afternoon evening night next get getting gets got gonna going will
ll yeah yes yep uh um hour hours minute minutes day days week weeks moment second
monday tuesday wednesday thursday friday saturday sunday couple few
dont know yet sure idea not no nope maybe think guess ok okay hmm
""".split())
# A bare answer ("Chicago.") is a place, but a question back at us is not.
_NOT_AN_ANSWER = re.compile(r"\b(?:what|where|when|how|why|who|which|paying|pays|rate)\b")

# Agreement, refusal and stalling — none of it is a place. Only consulted by the
# bare-answer fallback, where guessing wrong is worse than asking again.
_NOT_A_PLACE_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|no|nope|nah|ok|okay|sure|fine|works?|working|sounds?|"
    r"good|great|perfect|thanks|thank|deal|cover|covered|can|cannot|will|would|"
    r"should|maybe|dunno|know|think|guess|let|hang|hold|sec|second|minute|moment|"
    r"driver|truck|trailer|load|email)\b"
    r"|n't\b"
)


def extract_empty_when(text: str) -> str | None:
    """When the truck frees up — 'right now', 'tomorrow morning', '3 pm'."""
    match = _WHEN_RE.search(text.lower())
    return match.group().strip() if match else None


def _state_label(raw: str) -> str:
    return raw.upper() if len(raw) == 2 else raw.title()


def _is_filler(token: str) -> bool:
    # Compared without apostrophes so "I'm", "don't" and "truck's" are recognised.
    return token.replace("'", "").strip(".-") in _PLACE_FILLER


def _clean_place(raw: str) -> str | None:
    """Trim filler off both ends of a candidate place and title-case what's left."""
    tokens = [t for t in re.split(r"[\s,]+", raw.strip()) if t]
    while tokens and _is_filler(tokens[0]):
        tokens.pop(0)
    while tokens and _is_filler(tokens[-1]):
        tokens.pop()
    if not tokens or len(tokens) > 3:
        return None
    place = " ".join(t.strip(".") for t in tokens).strip(" '-")
    return place.title() if len(place) > 1 else None


def extract_empty_location(text: str) -> str | None:
    """Where the truck frees up. A state name — or a comma before a state
    abbreviation — is the strong signal; failing that, the words after
    'in' / 'out of' / 'near'; failing that, a short bare answer."""
    # Take the time half out of the way first, so "Joliet around 3pm" doesn't
    # come back as "Joliet Around".
    low = " ".join(_WHEN_RE.sub(" ", text.lower().replace("?", " ")).split())

    # "Towson, Arizona" / "Dallas, TX" — the comma settles it.
    match = re.search(rf"\b{_CITY},\s*({_STATE_NAMES}|{_STATE_ABBREVS})\b", low)
    if match and (city := _clean_place(match.group(1))):
        return f"{city}, {_state_label(match.group(2))}"

    # "empty in Phoenix Arizona" — a full state name needs no comma.
    match = re.search(rf"\b{_CITY}\s+({_STATE_NAMES})\b", low)
    if match and (city := _clean_place(match.group(1))):
        return f"{city}, {_state_label(match.group(2))}"

    # A bare state on its own: "empty in Arizona".
    if match := re.search(rf"\b({_STATE_NAMES})\b", low):
        return _state_label(match.group(1))

    # "in Joliet" / "out of Laredo" / "near Fontana".
    match = re.search(
        rf"\b(?:in|at|near|around|out of|outside of|outside)\s+{_CITY}", low)
    if match and (city := _clean_place(match.group(1))):
        return city

    # Just the answer: "Chicago." This is the only branch with no place signal in
    # it at all — no state, no preposition — so it needs its own guard. A caller
    # saying "yeah, that works" is agreeing with us, not naming a town called
    # Works, and recording that as their empty location poisons every later turn.
    # When in doubt return nothing: the agent then asks again, which is cheap.
    if not _NOT_AN_ANSWER.search(low) and not _NOT_A_PLACE_RE.search(low):
        return _clean_place(low)
    return None


def extract_money(text: str) -> float | None:
    """Extract a dollar amount: '$2,100', '2100', '2.1k'."""
    lowered = text.lower().replace(",", "")
    match = re.search(r"\$?\s*(\d{3,6})(?:\s*(?:dollars|bucks))?", lowered)
    if match:
        return float(match.group(1))
    kilo = re.search(r"(\d+(?:\.\d+)?)\s*k", lowered)
    if kilo:
        return float(kilo.group(1)) * 1000
    return None
