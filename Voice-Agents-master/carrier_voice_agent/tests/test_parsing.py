from lanevoice import parsing


def test_extract_load_id_variants():
    assert parsing.extract_load_id("about load L1001") == "L1001"
    assert parsing.extract_load_id("load 1002") == "L1002"
    assert parsing.extract_load_id("no numbers here") is None


def test_extract_mc_dot():
    assert parsing.extract_mc_dot("my MC is 123456") == ("MC", "123456")
    assert parsing.extract_mc_dot("USDOT 2000002") == ("DOT", "2000002")
    assert parsing.extract_mc_dot("it's 654321") == ("DOT", "654321")
    assert parsing.extract_mc_dot("no id") == (None, None)


def test_extract_money():
    assert parsing.extract_money("I need $2,100") == 2100
    assert parsing.extract_money("2050 dollars") == 2050
    assert parsing.extract_money("2.1k") == 2100
    assert parsing.extract_money("no price") is None


def test_is_probably_noise():
    # Whisper silence-hallucinations -> noise
    assert parsing.is_probably_noise("Thank you.") is True
    assert parsing.is_probably_noise("Thank you. Thank you.") is True
    assert parsing.is_probably_noise("you") is True
    assert parsing.is_probably_noise("So,") is True
    assert parsing.is_probably_noise("") is True
    # Real carrier input -> not noise
    assert parsing.is_probably_noise("L1001") is False
    assert parsing.is_probably_noise("MC 123456") is False
    assert parsing.is_probably_noise("yes") is False
    assert parsing.is_probably_noise("I need 2300") is False


def test_extract_email_and_phone():
    assert parsing.extract_email("send it to dispatch@blue.com please") == "dispatch@blue.com"
    assert parsing.extract_email("no email here") is None
    assert parsing.extract_phone("driver Mike 555-123-4567") == "555-123-4567"
    assert parsing.extract_phone("call (555) 123 4567") == "(555) 123 4567"
    assert parsing.extract_phone("no number") is None


def test_digits_read_one_at_a_time_are_understood():
    """From a live call: the agent asked for the number "one digit at a time",
    Whisper returned exactly that, and the consecutive-digits pattern matched
    nothing — so the agent asked again, and again, and the caller hung up."""
    assert parsing.extract_mc_dot("6, 5, 4, 3, 2, 1.") == ("DOT", "654321")
    assert parsing.extract_mc_dot("Hey.  It's 6, 5, 4, 3, 2, 1.") == ("DOT", "654321")
    assert parsing.extract_mc_dot("my MC is 6 5 4 3 2 1") == ("MC", "654321")
    assert parsing.extract_mc_dot("my DOT is 1-2-3-4-5-6") == ("DOT", "123456")


def test_digits_spoken_as_words_are_understood():
    assert parsing.extract_mc_dot("six five four three two one") == ("DOT", "654321")
    assert parsing.extract_mc_dot("USDOT one two three four five six") == ("DOT", "123456")
    assert parsing.extract_load_id("L one zero zero one") == "L1001"
    assert parsing.extract_load_id("load L, 1, 0, 0, 1") == "L1001"


def test_a_leaked_stt_prompt_does_not_hide_the_number():
    """Whisper echoed its own prompt in front of the caller's digits. The number
    is still in there and still theirs."""
    assert parsing.extract_mc_dot("Rates in dollars, 6, 5, 4, 3, 2, 1.") == (
        "DOT", "654321")


def test_quantities_are_never_glued_into_one_number():
    """The digit-joining must not touch weights, rates or counts."""
    for text in ("it's 42,000 lbs", "I need $2,150", "$2,150 and 26 pieces",
                 "925 miles", "2,487 miles at $6,800"):
        assert parsing.glue_spoken_digits(text) == text
    assert parsing.extract_money("I need $2,150") == 2150
    assert parsing.extract_money("it's 42,000 lbs") == 42000


# --------------------------------------------------------------------------- #
# Numeric load ids vs MC numbers
#
# Transport Pro load ids and MC numbers are both six or seven digits, so in
# numeric mode the MC/USDOT label is the only thing separating them. A caller
# asked for a load number very often answers with their MC instead, and there
# really are loads with six-digit ids — so reading "MC 556949" as a load number
# sends the agent off to look one up and then tell the caller their own MC isn't
# posted. Observed on a live call before this guard existed.
# --------------------------------------------------------------------------- #
def test_an_mc_labelled_number_is_never_a_load_id():
    for said in ("MC 556949", "my MC is 556949", "MC556949", "MC number 556949",
                 "mc# 556949"):
        assert parsing.extract_load_id(said, numeric=True) is None, said


def test_a_usdot_labelled_number_is_never_a_load_id():
    for said in ("USDOT 2999221", "US DOT 2999221", "DOT 2999221",
                 "my usdot is 2999221"):
        assert parsing.extract_load_id(said, numeric=True) is None, said


def test_an_unlabelled_or_load_labelled_number_still_reads_as_a_load():
    assert parsing.extract_load_id("load 2520571", numeric=True) == "2520571"
    assert parsing.extract_load_id("calling on 2520571", numeric=True) == "2520571"
    assert parsing.extract_load_id("2520571", numeric=True) == "2520571"
    assert parsing.extract_load_id("reference 1303369", numeric=True) == "1303369"
    assert parsing.extract_load_id(
        "one three zero three three six nine", numeric=True) == "1303369"


def test_the_load_number_wins_when_both_are_said_in_one_breath():
    """A carrier who volunteers both should still get their load looked up."""
    assert parsing.extract_load_id(
        "about load 2520571, my MC is 556949", numeric=True) == "2520571"
    assert parsing.extract_load_id(
        "MC 556949, calling on load 2520571", numeric=True) == "2520571"


def test_the_guard_does_not_change_how_an_mc_itself_is_read():
    """`extract_mc_dot` is the other half of the pair and must be untouched."""
    assert parsing.extract_mc_dot("MC 556949") == ("MC", "556949")
    assert parsing.extract_mc_dot("my MC is 556949") == ("MC", "556949")
    assert parsing.extract_mc_dot("USDOT 2999221") == ("DOT", "2999221")


def test_l_prefixed_mode_is_unaffected():
    """The seed data's L1001 form never had this ambiguity — the letter does the
    disambiguating there — so the guard must not touch it."""
    assert parsing.extract_load_id("about L1001") == "L1001"
    assert parsing.extract_load_id("MC 123456") == "L123456"
