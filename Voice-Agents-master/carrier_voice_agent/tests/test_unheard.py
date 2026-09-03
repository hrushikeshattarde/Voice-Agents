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


def test_comfort_noise_is_a_whisper_under_the_speech():
    """What the recogniser gets between the caller's words is no longer digital
    zero — a -55 dBFS hiss, enough to keep its speech detector awake, 35 dB under
    quiet speech — and nothing else about the frame changes. 0 dBFS means off."""
    import numpy as np
    from livekit import rtc

    from lanevoice.telephony.worker import with_comfort_noise

    silence = rtc.AudioFrame(data=bytes(2 * 480), sample_rate=48000, num_channels=1,
                             samples_per_channel=480)
    out = with_comfort_noise(silence, np.random.default_rng(0), -55.0)
    samples = np.frombuffer(out.data, dtype=np.int16).astype(np.float64)
    rms_dbfs = 20 * np.log10(np.sqrt((samples ** 2).mean()) / 32767)
    assert -58 < rms_dbfs < -52
    assert (out.sample_rate, out.num_channels, out.samples_per_channel) == (48000, 1, 480)
    assert with_comfort_noise(silence, np.random.default_rng(0), 0.0) is silence


def test_the_clock_fallback_does_not_invent_times():
    assert parsing.extract_empty_when("I'm in Lexington") is None
    assert parsing.extract_empty_when("about 42,000 pounds") is None


class _ReadsBackWhatItHeard:
    """A composer that does what a rep does with half a number: says it back."""

    def __init__(self):
        self.turns: list[dict] = []

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.turns.append({"directive": directive, "correction": correction})
        if "4177" not in str(dialogue):
            return "Circle Logistics, this is Alex, what can I help you with?"
        return "I got 4177 there — is that the whole number?"


def test_reading_back_the_callers_own_digits_is_not_a_money_leak(repo):
    """A caller gave a fragment ("4177"), the model read it back, and the reply
    guard rejected the read-back as money the turn was not given — three times,
    then handed the call off. The caller's own figures are not invented rates
    while a load is being identified."""
    settings = get_settings().model_copy(update={"numeric_load_ids": True})
    composer = _ReadsBackWhatItHeard()
    agent = CarrierSalesAgent(repo, composer, settings=settings)
    agent.greeting()
    agent.handle("it's 4177")
    assert agent.outcome is None                          # not handed off
    assert composer.turns[-1]["correction"] == ""         # accepted first time
    assert agent.state.value == "identify_load"
