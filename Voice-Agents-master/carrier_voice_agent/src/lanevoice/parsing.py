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


# "to"/"too" immediately in front of a digit run is a spoken "two" the
# transcriber mis-heard — observed twice on live calls ("Hello to 557062",
# "follow up to five six zero six six two"), each time costing the caller's
# leading digit and sending the agent after a DIFFERENT, real load. Only fixed
# when at least five digits follow, so "going to five stops" and "get to 3800"
# stay ordinary English — and only inside load-id extraction, never in the
# money or MC paths.
_DIGITISH_RUN = (r"((?:(?:zero|oh|one|two|three|four|five|six|seven|eight|nine"
                 r"|\d+)[\s,.\-]*)+)")


def _restore_leading_two(text: str) -> str:
    def fix(match: re.Match) -> str:
        digits = re.sub(r"\D", "", glue_spoken_digits(match.group(1)))
        if len(digits) >= 5:
            return "two " + match.group(1)
        return match.group(0)

    return re.sub(r"\b(?:to|too)\s+" + _DIGITISH_RUN, fix, text,
                  flags=re.IGNORECASE)


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
        spaced = glue_spoken_digits(_restore_leading_two(text)).upper()
        # Digit GROUPS the transcriber split — "255 6951", "2-566951" — glue to
        # one id when they sit adjacent with nothing but spaces or hyphens
        # between them. Tried FIRST, because the fragments of one spoken number
        # beat any single fragment: "2-566951" contains a six-digit run that
        # would match below, but the caller said seven digits. Comma-grouped
        # figures deliberately stay apart — "42,000 lbs" is a weight, and gluing
        # it would invent a five-digit load. Observed live: "255 6951" extracted
        # as nothing at all, and the agent asked for the number a caller had
        # just given, twice. A sentence break counts as a separator too: a caller
        # who pauses inside the number ("two five six … four one seven seven")
        # gets two transcripts from the streaming recogniser, and they arrive
        # joined as "256. 4177." — observed live, extracted as nothing, and the
        # call ended in a handoff over a number that had been said perfectly.
        for match in re.finditer(r"\d+(?:(?:[ -]+|\.\s+)\d+)+", spaced):
            digits = re.sub(r"\D", "", match.group())
            if 5 <= len(digits) <= 9 and not _labelled_as_carrier_id(
                    spaced, match.start()):
                return digits
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

# Words AND bare digit runs, because the forms that actually come back are MIXED:
# Whisper renders "twenty-six hundred" as "26 hundred", and Haiku 4.5 writes its
# own rates as "24 fifty". Scanning letters here and digits in `_NUMBER_RE` meant
# neither half saw the whole number — "26 hundred" read as {26, 100} and never as
# 2600 — so the money guardrail rejected correct turns and the carrier's own ask
# went unparsed. Measured with `tools/measure_latency.py`: 0 of 8 Haiku turns
# passed the guardrail before this, and every rejection costs a round trip.
_WORD_RE = re.compile(r"[a-z]+|\d+")


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
        if token.isdigit():
            # "24 fifty" -> chunks [24, 50], which the pair rule below reads as
            # 2450, exactly as it already read "twenty four fifty".
            values.append(int(token))
        elif token in _TENS_WORDS:
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
    # A run of nothing but digits is not this function's business: "42,000 lbs"
    # and "819 miles" are already read correctly by `_NUMBER_RE`, and pairing
    # their digits here would invent figures ("42" + "000" -> 4200) that the
    # money guardrail would then reject as leaks. At least one WORD is required,
    # which is what makes "26 hundred" mixed rather than bare.
    if not any(w.isalpha() for w in words):
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
        elif word.isdigit():
            current += int(word)      # "26 hundred" -> 26 * 100
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
        if (token.isdigit() or token in _NUMBER_WORDS or token in _TENS_WORDS
                or token in _SCALE_WORDS):
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

# A number written half in digits and half in words — "26 hundred", "24 fifty",
# "2 thousand". The head must be a scale or a tens word so that ordinary
# quantities are left alone: "24 pieces", "819 miles" and "12 p.m." do not match.
_MIXED_HEAD = "|".join(list(_SCALE_WORDS) + list(_TENS_WORDS))
_MIXED_TAIL = "|".join(list(_SCALE_WORDS) + list(_TENS_WORDS) + list(_NUMBER_WORDS))
_MIXED_NUMBER_RE = re.compile(
    rf"\b\d{{1,3}}\s+(?:{_MIXED_HEAD})\b(?:\s+(?:{_MIXED_TAIL})\b)*",
    re.IGNORECASE,
)


def fold_mixed_numbers(text: str) -> str:
    """Rewrite half-digit half-word numbers as plain digits: "24 fifty" -> "2450".

    Applied BEFORE any digit scan, and that ordering is the whole point. Reading
    the same text with a digit scanner and a word scanner gives one number three
    readings: "I'm holding at 24 fifty" yielded {24, 50} before the word scanner
    understood mixed forms, and {24, 2450} after — the leading fragment survives
    either way, and the money guardrail rejects the turn over the fragment. Fold
    first and there is exactly one reading of each figure.
    """
    def replace(match: re.Match) -> str:
        value = _run_value(_WORD_RE.findall(match.group().lower()))
        return str(value) if value is not None else match.group()

    return _MIXED_NUMBER_RE.sub(replace, text)


# A number the caller hung a unit on is a quantity, not part of their number.
# Observed live: an MC came back from the transcriber as "It's been six...  45
# minutes.", the digit scan read that as 645, and the agent reported it as
# progress — "I've got 6 4 5 so far, what comes after that?" — so the caller had
# to recite a number they had already given. Nobody measures anything in the
# middle of reading out an identifier, so a number with a unit behind it is
# never identifier digits. Plurals are spelled out per alternative: a trailing
# `s?` would let "minute" match inside "minutes" and leave the "s" stranded.
_QUANTITY_UNITS = (
    r"minutes?|mins?|hours?|hrs?|seconds?|secs?|days?|weeks?|months?|years?"
    r"|o'?clock|a\.?m\.?|p\.?m\.?|miles?|pounds?|lbs?|kilos?|tons?"
    r"|dollars?|bucks?|percent|cents?|stops?|trucks?|trailers?|pallets?"
    r"|pieces?|cases?|gallons?|feet|foot|inches|inch"
)
# `\d[\d,]*` and not `\d+`: without the comma the pattern eats "000 lbs" out of
# "42,000 lbs" and leaves a stray 42 behind, which is the same bug one digit
# smaller.
_QUANTITY_RE = re.compile(rf"\b\d[\d,]*\s*(?:{_QUANTITY_UNITS})\b",
                          re.IGNORECASE)


def heard_digits(text: str) -> str:
    """Every digit in an utterance, in order, with everything else dropped.

    Deliberately blunt: at the point we're trying to hear an identifier, "it's
    six five four" and "654" and "6-5-4" are the same thing, and we would rather
    hold three digits we can build on than nothing at all.

    Quantities are the one exception — see `_QUANTITY_RE`. A caller who says
    "I've been holding 45 minutes" has given us no digits at all.
    """
    spoken = _QUANTITY_RE.sub(" ", glue_spoken_digits(text))
    return re.sub(r"\D", "", spoken)


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


def _email_sound(text: str) -> str:
    """An address, or a stretch of speech about one, reduced to what survives a
    phone line and a recogniser: lowercase letters and digits only. "dispatch
    at circle delivers dot com", "dispatch@circledelivers.com" and "Dispatch,
    circle delivers.com" all become "dispatchcircledeliverscom"."""
    low = f" {text.lower()} "
    low = re.sub(r"\b(?:at|dot|period|point|underscore|under score|dash|hyphen|minus)\b", " ", low)
    return re.sub(r"[^a-z0-9]", "", low)


def match_spoken_email(text: str, candidates) -> str | None:
    """The address on the carrier's account that the caller most plausibly said.

    Observed live: "dispatch at circle delivers dot com" arrives from the
    recogniser as "Dispatch, circle delivers.com" — no "@", the domain in two
    words — and the exact parser hands back nothing, so a caller who read a real
    account address perfectly was asked again and then handed to a rep. The
    booking gate only ever sends to an address ALREADY on the account, so
    matching by sound against those addresses keeps the guarantee the gate
    exists for; the worst case is a link sent to another of the carrier's own
    addresses, and only when the whole of that address was heard.

    The whole normalised address has to appear in the normalised speech. A bare
    domain ("circle delivers dot com") matches nothing — that would pick one of
    many addresses at the same company by chance.
    """
    heard = _email_sound(text)
    if len(heard) < 6:
        return None
    hits = []
    for candidate in candidates:
        wanted = _email_sound(candidate)
        local = _email_sound(candidate.split("@", 1)[0])
        if wanted and wanted in heard and len(local) >= 3:
            hits.append(candidate)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # "asmith" is inside "zachary.smith" sound-wise only when the longer
        # one was said; prefer the longest match, and give up on a tie.
        hits.sort(key=lambda c: len(_email_sound(c)), reverse=True)
        if len(_email_sound(hits[0])) > len(_email_sound(hits[1])):
            return hits[0]
    return None


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

# Agreement, refusal, stalling and line-checking — none of it is a place. Only
# consulted by the bare-answer fallback, where guessing wrong is worse than
# asking again. The greeting row exists because of a live call: the caller,
# hearing dead air, said "Hello." — and the agent answered "Alright, Hello, got
# it" and treated Hello as the town their truck frees up in.
_NOT_A_PLACE_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|no|nope|nah|ok|okay|sure|fine|works?|working|sounds?|"
    r"good|great|perfect|thanks|thank|deal|cover|covered|can|cannot|will|would|"
    r"should|maybe|dunno|know|think|guess|let|hang|hold|sec|second|minute|moment|"
    r"driver|truck|trailer|load|email|"
    r"hello|hi|hey|howdy|goodbye|bye|alright|right|there|anybody|anyone|"
    r"hear|hearing|listen|listening|speak|speaking)\b"
    r"|n't\b"
)


# A clock time the way a formatting recogniser writes it: "10. A.m.", "2 p.m.",
# "8:30 am". `_WHEN_RE` reads "10 am"; the dotted forms — observed live from the
# streaming transcriber for a caller's "ten a.m." — it did not, so the agent asked
# for the time it had just been given.
_CLOCK_AMPM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\.?\s*([ap])\.?m\b\.?")


def extract_empty_when(text: str) -> str | None:
    """When the truck frees up — 'right now', 'tomorrow morning', '3 pm'."""
    match = _WHEN_RE.search(text.lower())
    if match:
        return match.group().strip()
    if clock := _CLOCK_AMPM_RE.search(text.lower()):
        hour, minute, half = clock.groups()
        return f"{hour}{':' + minute if minute else ''} {half}m"
    return None


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
# The separator is whatever the transcriber felt like: the live call gave "24/75"
# and the same line re-measured through `tools/measure_latency.py` gave "24-75".
# Accept the whole dash family, or the fix only covers the one that happened to
# be observed first.
#
# Both halves must be exactly two digits, which keeps "2.50 a mile", phone
# numbers ("555-111-2222" — no two-digit group sits against a separator) and
# single-digit dates ("8/17") out. A two-by-two date ("12/25") would still read
# as $1225 — accepted deliberately: it is far rarer on a rate turn than the form
# this exists for, and it fails safe, since a rate that far off the board rate
# goes to review rather than into a booking.
_SPLIT_RATE_RE = re.compile(r"\b(\d{2})\s*[/\-–—]\s*(\d{2})\b")


def extract_money(text: str) -> float | None:
    """Extract a dollar amount: '$2,100', '2100', '2.1k', 'twenty-four fifty'."""
    # "26 hundred" -> "2600" first, so the digit pattern below sees the whole
    # figure instead of the "26" in front of a word it doesn't read.
    lowered = fold_mixed_numbers(text.lower()).replace(",", "")
    match = re.search(r"\$?\s*(\d{3,6})(?:\s*(?:dollars|bucks))?", lowered)
    if match:
        return float(match.group(1))
    kilo = re.search(r"(\d+(?:\.\d+)?)\s*k", lowered)
    if kilo:
        return float(kilo.group(1)) * 1000
    if split := _SPLIT_RATE_RE.search(lowered):
        return float(f"{split.group(1)}{split.group(2)}")
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
