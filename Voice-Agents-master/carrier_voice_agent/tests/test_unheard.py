"""Speech the recogniser returned empty must not end in silence.

On the first live call of the streaming pipeline a caller answered "10 AM" four
times — half a second each, quietly — and the recogniser returned an empty
transcript every time. No turn was committed, nothing was said, they hung up.
The worker's watchdog now asks them to repeat; what is pinned here is the record
it leaves and the time parser reading the transcript that DID come through.
"""

from lanevoice import parsing
from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer


def test_an_unheard_turn_is_noted_on_the_call_not_in_the_dialogue(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    agent.note_unheard()
    conn = repo._db.connect()
    try:
        notes = [r[0] for r in conn.execute("SELECT note FROM call_notes").fetchall()]
    finally:
        conn.close()
    assert any("nothing was transcribed" in note for note in notes)
    # The dialogue the composer reads is untouched — the caller said nothing usable.
    assert [who for who, _ in agent.transcript] == ["agent"]


def test_a_dotted_clock_time_is_read_as_a_time():
    assert parsing.extract_empty_when("10. A.m.") == "10 am"
    assert parsing.extract_empty_when("2 p.m.") == "2 pm"
    assert parsing.extract_empty_when("8:30 A.M. tomorrow") == "tomorrow"   # _WHEN_RE first
    assert parsing.extract_empty_when("10 AM.") == "10 am"


def test_the_clock_fallback_does_not_invent_times():
    assert parsing.extract_empty_when("I'm in Lexington") is None
    assert parsing.extract_empty_when("about 42,000 pounds") is None
