"""Where a streamed reply may be cut for the voice — and where it must not be."""

from lanevoice.voice.sentences import split_sentences


def test_complete_sentences_come_off_the_front_and_the_rest_waits():
    done, rest = split_sentences("Got it. Load 2532717, that the one? Alright, so")
    assert done == ["Got it.", "Load 2532717, that the one?"]
    assert rest == "Alright, so"


def test_nothing_is_cut_until_the_next_sentence_has_started():
    # The period is there, but the model may still be writing; only the space
    # and a capital after it prove the sentence ended.
    assert split_sentences("Got it.") == ([], "Got it.")
    assert split_sentences("Got it. ") == ([], "Got it. ")
    assert split_sentences("Got it. A") == (["Got it."], "A")


def test_decimals_abbreviations_and_initials_are_not_boundaries():
    assert split_sentences("It pays $2.50 a mile. That work?") == (
        ["It pays $2.50 a mile."], "That work?")
    assert split_sentences("Runs to St. Louis on Monday. Then") == (
        ["Runs to St. Louis on Monday."], "Then")
    # "a.m." is never a cut, even before a capital: "10 a.m. Monday" is one
    # thought, and a later start is the cheap mistake, a pause mid-thought is not.
    assert split_sentences("Empty at 10 a.m. Monday. Then") == (
        ["Empty at 10 a.m. Monday."], "Then")
    assert split_sentences("Empty at 10 a.m. Alright, so") == ([], "Empty at 10 a.m. Alright, so")
    assert split_sentences("Talk to J. Smith about it. He") == (
        ["Talk to J. Smith about it."], "He")


def test_spoken_times_dates_and_numbers_split_where_a_rep_would_pause():
    text = ("Picks up Friday the 7th. Delivers Monday the 10th. "
            "It's about 225 miles from you. Got")
    done, rest = split_sentences(text)
    assert done == ["Picks up Friday the 7th.", "Delivers Monday the 10th.",
                    "It's about 225 miles from you."]
    assert rest == "Got"
    assert split_sentences("Empty at 10 AM. Alright") == (["Empty at 10 AM."], "Alright")


def test_quotes_and_exclamations_end_a_sentence_too():
    assert split_sentences('He said "book it." Alright') == (['He said "book it."'], "Alright")
    assert split_sentences("Good deal! So for this one") == (["Good deal!"], "So for this one")


def test_a_lowercase_continuation_is_not_a_boundary():
    text = "we run it every week. so you're first"
    assert split_sentences(text) == ([], text)
