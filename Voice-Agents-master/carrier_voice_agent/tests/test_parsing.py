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


def test_a_rate_said_the_way_a_rep_says_it():
    """"Twenty-four seventy-five" is how a rate gets said out loud, and Whisper
    writes it as the fraction "24/75". Neither form used to parse at all.

    The live cost: the ask never reached the negotiator, the turn was handled as
    "they gave me no number", and the composer — having just read the caller say
    2475 — spoke it, was rejected three times for naming unauthorised money, and
    the call was handed to a rep. Load 2513446.
    """
    assert parsing.extract_money("24/75") == 2475
    assert parsing.extract_money("twenty four seventy five") == 2475
    assert parsing.extract_money("twenty-four fifty") == 2450
    assert parsing.extract_money("I can do twenty five hundred") == 2500
    assert parsing.extract_money("two thousand four hundred") == 2400


def test_words_that_are_not_rates_are_not_heard_as_rates():
    """The word path runs on every negotiation turn, so a false positive here is
    a rate the carrier never asked for."""
    # Parses as the number 100, which is not an offer of $100.
    assert parsing.extract_money("a couple hundred more") is None
    assert parsing.extract_money("I've got two drivers") is None
    assert parsing.extract_money("no") is None
    assert parsing.extract_money("that works for me") is None
    # Two rates and no way to tell which is the operative ask -> ask them, don't
    # guess. This is the pre-existing behaviour, kept deliberately.
    assert parsing.extract_money(
        "I was at twenty six hundred but I'll take twenty five hundred") is None
    # Digits still win outright when they are there.
    assert parsing.extract_money("I'll do 2500, not twenty six hundred") == 2500


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


def test_a_rate_written_half_in_digits_half_in_words():
    """"26 hundred" and "24 fifty" are what BOTH sides of the call actually
    produce — Whisper renders "twenty-six hundred" that way, and the composer
    writes its own rates that way.

    Before this, a digit scanner and a word scanner read the same figure
    separately: "26 hundred" came out as {26, 100}, never 2600. That broke the
    money guardrail on correct turns (measured: 0 of 8 composed turns passed) and
    left the carrier's own ask unparsed.
    """
    assert parsing.extract_money("26 hundred") == 2600
    assert parsing.extract_money("24 fifty") == 2450
    assert parsing.extract_money("I can do 2 thousand") == 2000
    assert parsing.fold_mixed_numbers("holding at 24 fifty") == "holding at 2450"
    assert parsing.fold_mixed_numbers("can't get to 26 hundred") == "can't get to 2600"


def test_folding_leaves_ordinary_quantities_alone():
    """The fold runs over every agent reply before the money guardrail reads it,
    so a figure invented here is a correct turn rejected as a rate leak."""
    for text in ("it's 42,000 lbs", "819 miles", "load 2513446", "24 pieces",
                 "picking up at 12 p.m.", "40,000 pounds on 24 pallets"):
        assert parsing.fold_mixed_numbers(text) == text, text
    # And the plain readings still come through unchanged.
    assert parsing.extract_money("it's 42,000 lbs") == 42000
    assert parsing.extract_money("819 miles") == 819


def test_one_figure_gets_exactly_one_reading():
    """The fold exists so the two scanners cannot disagree. "24 fifty" must read
    as 2450 and NOT also leave a bare 24 behind — the leading fragment is what
    the guardrail used to reject."""
    from lanevoice.conversation.agent import _numbers

    assert _numbers("I'm holding at 24 fifty") == {2450}
    assert _numbers("can't get to 26 hundred, holding at 24 fifty") == {2600, 2450}
    # A genuine bare abbreviation is still caught: "the 26" is not $2600, and the
    # voice would say "twenty-six".
    assert 26 in _numbers("what's driving the 26 on this one?")
