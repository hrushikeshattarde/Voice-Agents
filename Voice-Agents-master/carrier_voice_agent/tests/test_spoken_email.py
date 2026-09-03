"""An address read down the phone is matched against the carrier's account by
sound, so a recogniser that drops the "@" or splits the domain into words does
not cost a verified carrier their booking — and nothing off the account can
ever match.
"""

from lanevoice import parsing

ON_FILE = ("dispatch@circledelivers.com", "billing@circledelivers.com",
           "asmith@circledelivers.com", "zachary.smith@circledelivers.com",
           "ajsmith616@gmail.com")


def _match(said: str) -> str | None:
    return parsing.match_spoken_email(said, ON_FILE)


def test_a_spoken_address_is_matched_by_sound():
    assert _match("Dispatch, circle delivers.com") == "dispatch@circledelivers.com"
    assert _match("it's billing at circle delivers dot com") == "billing@circledelivers.com"
    assert _match("a j smith 6 1 6 at gmail dot com") == "ajsmith616@gmail.com"


def test_the_longer_of_two_nested_addresses_wins():
    assert (_match("zachary dot smith at circledelivers dot com")
            == "zachary.smith@circledelivers.com")
    assert _match("a smith at circledelivers.com") == "asmith@circledelivers.com"


def test_nothing_off_the_account_matches():
    assert _match("hrushikesh at circledelivers dot com") is None
    assert _match("Red circle delivers.com") is None
    assert _match("circle delivers dot com") is None        # a bare domain picks nobody
    assert _match("yes") is None
