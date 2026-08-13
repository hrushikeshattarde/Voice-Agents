"""
Numbers the agent says in WORDS — and the money guardrail that couldn't see them.

Told to sound like a freight desk, the model states rates the way a rep does:
"I'm at twenty-four fifty on this one." A digit-only scan reads that sentence as
containing no numbers at all. Measured 6 out of 6 on one live turn, which is why
retrying never helped — the model was consistently right and the checker
consistently wrong.

Both halves of the guardrail were broken by it, and the second is the one that
mattered:

  * `must_say` REJECTED that correct turn three times and handed the call to a rep.
  * `_rate_leak` WAVED THROUGH "I can do twenty-six hundred" when only $2450 was
    authorised. The central claim of this codebase — that a hallucinated rate never
    reaches a carrier's ear — did not hold for any figure spelled out in words.

The parser is deliberately additive: a form it fails to recognise leaves the old
digit-only behaviour rather than a new hole. What it must never do is invent a
number out of ordinary speech, which is what the pronoun cases below guard.
"""

import pytest

from lanevoice.conversation.agent import _breach, _numbers, _rate_leak
from lanevoice.parsing import spoken_numbers

SOURCE = "you're asking $2450 on this one — your number, not theirs"


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("said,expected", [
    # The rate idiom — two chunks, juxtaposed. This is the one that broke the call.
    ("twenty-four fifty", 2450),
    ("twenty four fifty", 2450),
    ("twenty-six hundred", 2600),
    ("twenty-nine fifty", 2950),
    ("eighteen fifty", 1850),
    ("nine fifty", 950),
    ("two fifty", 250),
    # Scale words.
    ("sixteen hundred", 1600),
    ("eight hundred", 800),
    ("two thousand", 2000),
    ("two thousand four hundred fifty", 2450),
    ("two thousand and fifty", 2050),
    ("three grand", 3000),
    # Plain small numbers, which the requirements turn is full of.
    ("thirty", 30),
    ("less than ten years old", 10),
    ("twenty four", 24),
    ("seven", 7),
])
def test_a_spoken_number_is_read(said, expected):
    assert expected in spoken_numbers(said)


@pytest.mark.parametrize("said", [
    # Pronouns, not numbers. Constant on a freight desk, and reading each as 1
    # made every ordinary sentence look like it named an unauthorised figure.
    "on this one", "which one of those works for you", "one moment",
    "no one has it", "I'll take that one",
    # No numbers at all.
    "no holes, food grade, swing doors", "clean and dry",
    "", "   ",
])
def test_ordinary_speech_yields_no_number(said):
    assert spoken_numbers(said) == set()


def test_a_read_back_digit_string_is_not_a_number():
    """"one seven nine eight four one four" is somebody reading an MC out. Folding
    it into a single value would be nonsense, and `glue_spoken_digits` is what
    handles that shape."""
    assert spoken_numbers("I've got one seven nine eight four one four") == set()


def test_a_neighbour_rescues_a_pronoun_word():
    """Only a LONE "one" is skipped — with a neighbour, a number was plainly meant."""
    assert 1000 in spoken_numbers("one thousand")
    assert 21 in spoken_numbers("twenty-one")
    assert 150 in spoken_numbers("one fifty")
    assert 100 in spoken_numbers("one hundred")


def test_several_numbers_in_one_sentence_are_all_found():
    said = ("detention's thirty an hour after two hours, up to seven hours, then a "
            "two fifty layover")
    found = spoken_numbers(said)
    assert {30, 2, 7, 250} <= found


# --------------------------------------------------------------------------- #
# Face one: a correct turn is no longer rejected
# --------------------------------------------------------------------------- #
def test_the_turn_that_lost_a_call_now_passes():
    """Verbatim from the live composer, reproduced 6/6 before the fix."""
    said = "Alright, so I'm at twenty-four fifty on this one. Does that work for you?"
    assert 2450 in _numbers(said)
    assert _breach(said, {2450}, 2450, SOURCE, "$2450") is None


@pytest.mark.parametrize("said", [
    "I'm at twenty-four fifty on this one.",
    "I've got it at two thousand four hundred fifty.",
    "Twenty-four fifty is where I'm at.",
    "I can do $2450.",
    "I'm at 2450 on this load.",
])
def test_every_way_of_saying_our_own_number_satisfies_must_say(said):
    assert _breach(said, {2450}, 2450, SOURCE, "$2450") is None


# --------------------------------------------------------------------------- #
# Face two: a rate nobody authorised no longer slips through
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("said", [
    "I can do $2600 on it",
    "I can do twenty-six hundred on it",
    "tell you what, three grand and it's yours",
    "I'll go to twenty-nine fifty",
    "how about two thousand six hundred",
    "I'll meet you at eighteen fifty",
])
def test_an_unauthorised_rate_is_caught_however_it_is_written(said):
    assert _rate_leak(said, {2450}, SOURCE) is True


def test_the_authorised_rate_is_not_a_leak_in_either_form():
    for said in ("I'm at twenty-four fifty", "I'm at $2450", "I'm at 2450"):
        assert _rate_leak(said, {2450}, SOURCE) is False, said


def test_a_figure_the_directive_mentions_is_still_speakable():
    """Numbers reach the model through FACTS as well — weights, miles, piece
    counts. Those stay sayable in words, or the load pitch would breach on itself."""
    source = "Miles: 819. Weight: 40,000 lbs. Pieces: 24."
    said = ("that's eight hundred nineteen miles, forty thousand pounds, "
            "twenty-four pieces")
    assert _rate_leak(said, set(), source) is False


def test_an_appointment_time_is_not_read_as_a_rate():
    """"8:30" spoken is "eight thirty", which the pair form reads as 830. Emitting
    it from the source too is what stops a pickup window breaching."""
    source = "Pickup window: 8:30 AM to 4 PM"
    assert _rate_leak("you can get there at eight thirty", set(), source) is False
