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


# An MC or USDOT label sitting just in front of a number. Transport Pro load ids
# and MC numbers are both six or seven digits, so in numeric mode the label is the
# only thing that tells them apart — and a caller who has just been asked for a
# load number will often answer with their MC instead ("MC 556949"). Reading that
# as a load number is worse than hearing nothing: there really are loads with
# six-digit ids, so the agent goes off and looks one up, then tells the caller
# their own MC number isn't posted.
_CARRIER_ID_LABEL_RE = re.compile(r"\bMC\b|\bMC(?=\d)|US ?DOT|\bDOT\b")


def _labelled_as_carrier_id(upper_text: str, at: int) -> bool:
    """True if the number starting at `at` is introduced as an MC or USDOT.

    The same 25-character lookback `extract_mc_dot` classifies with, so the two
    agree on what counts as labelled — "MC 556949" and "my MC is 556949" both do,
    and filler words between the label and the digits don't defeat it.
    """
    return bool(_CARRIER_ID_LABEL_RE.search(upper_text[max(0, at - 25):at]))


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
    if numeric:
        # Keep the spacing: the MC/USDOT label has to stay adjacent to its digits
        # for the guard below, and stripping spaces glues "MC" onto the number.
        spaced = glue_spoken_digits(text).upper()
        for match in re.finditer(r"\d{5,9}", spaced):
            if not _labelled_as_carrier_id(spaced, match.start()):
                return match.group()
        return None
    compact = glue_spoken_digits(text).upper().replace(" ", "")
    match = re.search(r"L?\d{4,6}", compact)
    if not match:
        return None
    token = match.group()
    digits = re.sub(r"\D", "", token)
    return token if token.startswith("L") else f"L{digits}"



# --------------------------------------------------------------------------- #
# Numbers written as WORDS
#
# The agent's own replies are the reason this exists. Told to sound like a freight
# desk, the model states rates the way a rep does — "I'm at twenty-four fifty on
# this one" — and a digit-only scan finds nothing in that sentence. Measured 6 out
# of 6 on one live turn.
#
# That cut both ways, and the second way was the dangerous one:
#   * the "did you state OUR number" check rejected a perfectly correct turn three
#     times and handed the call to a rep;
#   * the "did you invent a number" check waved through "I can do twenty-six
#     hundred" when only $2450 was authorised — the money guardrail was bypassable
#     by spelling the figure out.
# --------------------------------------------------------------------------- #
_NUMBER_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
# "grand" is how a rate gets rounded off out loud: "three grand".
_SCALE_WORDS = {"hundred": 100, "thousand": 1000, "grand": 1000, "k": 1000}
_FILLER_WORDS = {"and", "a"}
# Words that are number words in a number and PRONOUNS everywhere else. On a
# freight desk "this one", "which one of those" and "one moment" are constant, and
# reading each as the figure 1 made every ordinary sentence look like it contained
# an unauthorised amount. Only skipped when the word stands ALONE — "one thousand",
# "twenty-one" and "one fifty" all still parse, because there a neighbour settles
# that a number was meant.
_PRONOUN_NUMBERS = {"one", "oh"}

_WORD_RE = re.compile(r"[a-z]+")


def _chunk(tokens: list[str]) -> list[int]:
    """A run of number words -> the plain values it is built from.

    A chunk is one ordinary number under a hundred: a teen, a tens word with an
    optional unit, or a bare unit. `"twenty four fifty"` is TWO chunks (24, 50),
    while `"twenty four"` is one (24) — which is the whole distinction the pair
    form below rests on.
    """
    values: list[int] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _TENS_WORDS:
            value = _TENS_WORDS[token]
            if index + 1 < len(tokens) and tokens[index + 1] in _NUMBER_WORDS \
                    and _NUMBER_WORDS[tokens[index + 1]] < 10:
                value += _NUMBER_WORDS[tokens[index + 1]]
                index += 1
            values.append(value)
        elif token in _NUMBER_WORDS:
            values.append(_NUMBER_WORDS[token])
        index += 1
    return values


def _run_value(tokens: list[str]) -> int | None:
    """One run of number words -> the number a person meant by it."""
    words = [w for w in tokens if w not in _FILLER_WORDS]
    if not words:
        return None
    if len(words) == 1 and words[0] in _PRONOUN_NUMBERS:
        return None
    scales = [w for w in words if w in _SCALE_WORDS]

    if not scales:
        chunks = _chunk(words)
        if not chunks:
            return None
        if len(chunks) == 1:
            return chunks[0]
        # The rate idiom: "twenty-four fifty" is 2450, not 74. Only ever two
        # chunks — "one two three" is somebody reading digits, not a number, and
        # `glue_spoken_digits` already handles that case elsewhere.
        if len(chunks) == 2 and all(0 <= c < 100 for c in chunks):
            return chunks[0] * 100 + chunks[1]
        return None

    # Ordinary English with scale words: "two thousand four hundred fifty".
    total = current = 0
    for word in words:
        if word in _SCALE_WORDS:
            scale = _SCALE_WORDS[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
        elif word in _TENS_WORDS:
            current += _TENS_WORDS[word]
        elif word in _NUMBER_WORDS:
            current += _NUMBER_WORDS[word]
    return (total + current) or None


def spoken_numbers(text: str) -> set[int]:
    """Every number the text states in WORDS. `{}` when it states none.

    Deliberately additive: it never removes anything a digit scan found, so a
    parse this misses leaves behind exactly the previous behaviour rather than a
    new hole.
    """
    tokens = _WORD_RE.findall(str(text or "").lower().replace("-", " "))
    found: set[int] = set()
    run: list[str] = []
    for token in tokens:
        if token in _NUMBER_WORDS or token in _TENS_WORDS or token in _SCALE_WORDS:
            run.append(token)
            continue
        # A filler only stays inside a run that has already started.
        if token in _FILLER_WORDS and run:
            run.append(token)
            continue
        if (value := _run_value(run)) is not None:
            found.add(value)
        run = []
    if (value := _run_value(run)) is not None:
        found.add(value)
    return found

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

# The two lists above are PARALLEL — alabama/al, alaska/ak, … — so the name ->
# code map is zipped out of them rather than written a third time. `strict=True`
# is the guard: adding a state to one list and not the other fails at import
# rather than silently mis-coding a state on a live call.
STATE_CODES: dict[str, str] = {
    name: abbrev.upper()
    for name, abbrev in zip(_STATE_NAMES.split("|"), _STATE_ABBREVS.split("|"),
                            strict=True)
}


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


# The smallest figure worth reading as a rate. Matches the 3-digit minimum the
# digit pattern below already enforces, and it is what keeps "a couple hundred
# more" (which parses as the number 100) from being heard as a $100 ask — an ask
# that low trips the fraud tripwire and ends the call at a human.
_MIN_RATE = 300

# "twenty-four seventy-five" is how every rep on the phone says $2475, and
# Whisper renders it as the fraction "24/75". Nothing in the digit or word paths
# below sees a number in that at all, so the turn reached the negotiator as "they
# gave me no rate" — and the composer, having just read the caller say it, spoke
# $2475, was rejected three times for naming money it wasn't given, and the call
# was handed to a rep. Observed live on load 2513446.
#
# Both halves must be exactly two digits, which keeps "2.50 a mile" and single-
# digit dates ("8/17") out. A two-by-two date ("12/25") would still read as
# $1225 — accepted deliberately: it is far rarer on a rate turn than the form
# this exists for, and it fails safe, since a rate that far off the board rate
# goes to review rather than into a booking.
_SLASHED_RATE_RE = re.compile(r"\b(\d{2})\s*/\s*(\d{2})\b")


def extract_money(text: str) -> float | None:
    """Extract a dollar amount: '$2,100', '2100', '2.1k', 'twenty-four fifty'."""
    lowered = text.lower().replace(",", "")
    match = re.search(r"\$?\s*(\d{3,6})(?:\s*(?:dollars|bucks))?", lowered)
    if match:
        return float(match.group(1))
    kilo = re.search(r"(\d+(?:\.\d+)?)\s*k", lowered)
    if kilo:
        return float(kilo.group(1)) * 1000
    if slashed := _SLASHED_RATE_RE.search(lowered):
        return float(f"{slashed.group(1)}{slashed.group(2)}")
    # Said in words. `spoken_numbers` already knows the rate idiom — "twenty-four
    # fifty" is 2450, not 74 — and it is the same reader the money guardrail uses
    # on the agent's own replies, so both sides of the call read a spoken rate the
    # same way.
    #
    # ONLY when it finds exactly one plausible rate. A set has no order, so
    # "I was at twenty-six hundred, I'll take twenty-five hundred" gives no way to
    # tell the operative ask from the one they just abandoned. Returning None
    # there is the previous behaviour — the agent asks what their number is —
    # which is a great deal better than booking the wrong one.
    spoken = {n for n in spoken_numbers(lowered) if n >= _MIN_RATE}
    if len(spoken) == 1:
        return float(spoken.pop())
    return None
