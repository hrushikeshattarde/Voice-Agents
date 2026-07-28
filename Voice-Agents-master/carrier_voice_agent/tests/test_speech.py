"""What reaches the voice, as opposed to what reaches the transcript.

These two diverge, and that is exactly what makes the class of bug below so
unpleasant: the logs look perfect while the caller hears something else. Every
case here came off a real call.
"""

from lanevoice import formatting
from lanevoice.voice.tts import speechify


def test_hyphenated_identifiers_are_respaced():
    """From a live call: the transcript read "L-1-0-0-3" and the caller heard
    "L 1 0 0 0 0 3". The voice takes the hyphens as part of the number."""
    assert speechify("I've got the load number as L-1-0-0-3.") == \
        "I've got the load number as L 1 0 0 3."
    assert speechify("your MC is 6-5-4-3-2-1, right?") == \
        "your MC is 6 5 4 3 2 1, right?"


def test_glued_identifiers_are_still_spelled_out():
    assert speechify("about L1002") == "about L 1 0 0 2"


def test_spell_digits_and_the_voice_agree():
    """`spell_digits` output goes into the composer's facts and usually reaches
    the voice verbatim, so it must already be in the form the voice reads."""
    spelled = formatting.spell_digits("L1003")
    assert spelled == "L 1 0 0 3"
    assert speechify(spelled) == spelled       # nothing left to rewrite


def test_things_that_only_look_like_spelled_digits_are_left_alone():
    """Each element has to be a lone digit, or phone numbers and time windows
    would get read out one character at a time."""
    assert speechify("call me on 555-111-2222") == "call me on 555-111-2222"
    assert speechify("picks up 8-10 AM") == "picks up 8-10 AM"
    assert speechify("it's 42,000 lbs") == "it's 42,000 lbs"


def test_whole_dollar_rates_are_spoken_as_words():
    assert "dollars" in speechify("I've got it at $2150.")
    assert "$" not in speechify("I've got it at $2150.")


def test_a_per_mile_rate_does_not_strand_its_cents():
    """The old pattern stopped at the dollars: "$2.50 a mile" reached the voice
    as "two dollars.50 a mile"."""
    spoken = speechify("I need $2.50 a mile")
    assert ".50" not in spoken
    assert spoken == "I need two fifty a mile"


def test_cents_on_a_full_rate_are_spoken_as_cents():
    assert speechify("$2150.75") == "two thousand, one hundred and fifty dollars " \
                                    "seventy-five cents"
    assert speechify("$2150.00") == "two thousand, one hundred and fifty dollars"
