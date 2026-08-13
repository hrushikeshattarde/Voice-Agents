"""
The turn that stopped mid-sentence, and the "Hello." that counted as yes.

One live call, three defects in a chain:

  1. The pitch and the load's shipper instructions were composed as ONE turn. On a
     load with long board notes that ran past `LLM_MAX_TOKENS` and was cut off mid
     sentence — "...you'll get paid a hundred bucks for" — and spoken to the caller
     exactly like that. Nothing noticed: `finish_reason` was never read, so a
     truncated reply is indistinguishable from a finished one.

  2. The caller, reasonably, said "Hello." — they thought the line had dropped.
     The requirements gate accepted anything that was not an explicit refusal, so
     "Hello." was recorded as them confirming a food-grade trailer, swing doors and
     a unit under ten years old.

  3. Now in the pricing state, the model kept answering "Hello." conversationally
     instead of quoting the rate, failed `must_say` three times, and the call went
     to a rep.

Defect 2 is the dangerous one — a driver turned away at a dock over a condition
they never agreed to. Defect 1 is what caused all of it.
"""

import pytest

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.conversation.agent import (
    _MAX_REQUIREMENT_ASKS,
    _confirms_requirements,
    _declines_requirements,
)
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer
from lanevoice.voice.composer import OpenRouterComposer

EMPTY = "empty in Dallas, Texas today"


# --------------------------------------------------------------------------- #
# 1. A turn that ran out of tokens
# --------------------------------------------------------------------------- #
class _Truncating:
    """Reports truncation for the first `n` attempts, as a real API does."""

    def __init__(self, n: int):
        self.n = n
        self.calls: list[str] = []

    def __call__(self, system, user, *, max_tokens, temperature, json_mode=False):
        self.calls.append(user)
        cut = len(self.calls) <= self.n
        return ("Alright, so that load picks up Monday and you'll get paid a "
                "hundred bucks for", cut) if cut else ("Alright, that works.", False)


def _composer(chat):
    composer = OpenRouterComposer.__new__(OpenRouterComposer)
    composer._settings = get_settings().model_copy(
        update={"llm_max_tokens": 220, "llm_read_max_tokens": 120})
    composer._model = "test"
    composer._chat = chat
    return composer


def test_a_cut_off_turn_is_retried_with_an_instruction_to_be_shorter():
    """The same prompt would be cut off again, so the retry has to SAY something
    different — that is why it lives in the composer and not in `_say`'s loop."""
    chat = _Truncating(n=1)
    said = _composer(chat).compose("Give them the load.")

    assert said == "Alright, that works."
    assert len(chat.calls) == 2
    assert "RAN PAST THE LENGTH LIMIT" in chat.calls[1]
    assert "FAR fewer words" in chat.calls[1]
    # The original instruction is still there — it is the same turn, said shorter.
    assert "Give them the load." in chat.calls[1]


def test_a_turn_cut_off_twice_is_refused_rather_than_half_said():
    """Half a sentence reaching a driver is worse than no turn at all. Returning
    "" is what makes `_say` re-prompt and then hand the call to a rep."""
    chat = _Truncating(n=99)
    assert _composer(chat).compose("Give them the load.") == ""
    assert len(chat.calls) == 2, "should not keep retrying while a carrier waits"


def test_an_untruncated_turn_costs_exactly_one_call():
    chat = _Truncating(n=0)
    assert _composer(chat).compose("Say hello.") == "Alright, that works."
    assert len(chat.calls) == 1


def test_truncated_extraction_degrades_to_nothing_said():
    """Truncated JSON does not parse, and a failed extraction must read as "they
    didn't say" rather than a guess."""
    chat = _Truncating(n=99)
    got = _composer(chat).read("some dialogue", {"city": "where", "when": "when"})
    assert got == {"city": None, "when": None}


def test_the_provider_reports_truncation_from_finish_reason():
    """`finish_reason == "length"` is the only signal that a reply is incomplete —
    the text itself is grammatical and plausible right up to where it stops."""
    import httpx

    def handler(_request):
        return httpx.Response(200, json={
            "id": "cmpl-1", "object": "chat.completion", "created": 0,
            "model": "test",
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": "cut off here"}}],
        })

    settings = get_settings().model_copy(update={"openrouter_api_key": "k"})
    composer = OpenRouterComposer(settings)
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    text, truncated = composer._chat("sys", "user", max_tokens=10, temperature=0.5)
    assert text == "cut off here"
    assert truncated is True


# --------------------------------------------------------------------------- #
# 2. Consent has to be given, not merely not-withheld
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("said", [
    "Hello.", "hello? you there", "what?", "sorry, say that again",
    "uh", "hmm", "the line cut out", "", "   ", "I'm in Dallas",
])
def test_a_non_answer_is_not_consent(said):
    """Every one of these used to move the call to a rate. "Hello." is the one that
    actually happened."""
    assert _confirms_requirements(said) is False
    assert _declines_requirements(said) is False, said


@pytest.mark.parametrize("said", [
    "yes", "yeah we can do that", "yep", "sure", "no problem", "we can do all that",
    "that's fine", "ok", "understood", "10-4", "absolutely", "sounds good",
])
def test_a_real_yes_is_consent(said):
    assert _confirms_requirements(said) is True


@pytest.mark.parametrize("said", [
    "no", "we can't do that", "nope", "not equipped for that",
    "we don't have a food grade trailer",
])
def test_a_real_no_is_still_a_decline(said):
    assert _declines_requirements(said) is True


# --------------------------------------------------------------------------- #
# 3. The gate, end to end
# --------------------------------------------------------------------------- #
def _to_requirements(repo):
    """A call parked at the requirements gate, on a load that has board notes."""
    settings = get_settings().model_copy(update={"max_negotiation_rounds": 6})
    agent = CarrierSalesAgent(repo, StubComposer(), settings=settings)
    agent.greeting()
    agent.handle("L1002")          # seeded WITH special requirements
    agent.handle("MC 123456")
    agent.handle(EMPTY)
    assert agent.state.value == "check_requirements"
    return agent


def test_the_pitch_no_longer_reads_the_requirements(repo):
    """Two turns, because one ran to 25 seconds of speech and hit the limit."""
    agent = _to_requirements(repo)
    directive = agent._composer.turns[-1]["directive"]
    assert "Do NOT read them the special requirements yet" in directive
    assert "specific requirements" in directive


def test_the_pitch_is_held_to_a_phone_length(repo):
    """The rundown is capped, and the screen detail is left for the asking.

    Uncapped it listed every field on the record — both appointment windows, the
    piece count, the dimensions — and ran 27 seconds before the carrier was asked
    anything. None of it is gone: it stays in FACTS, so a carrier who wants the
    piece count gets it on the turn they ask for it.
    """
    agent = _to_requirements(repo)
    directive = agent._composer.turns[-1]["directive"]
    assert "THREE SENTENCES AT MOST" in directive
    assert "LEAVE OUT unless they ask" in directive
    # The lane, the days and the equipment still have to be said.
    assert "the lane (both cities)" in directive
    assert "pickup day and the delivery day" in directive
    # And the detail is deferred, not dropped — it is still on the record.
    facts = agent._composer.turns[-1]["facts"]
    assert "Pieces" in facts or "Pickup window" in facts


def test_the_requirements_are_read_in_their_own_turn(repo):
    agent = _to_requirements(repo)
    agent.handle("go ahead")

    directive = agent._composer.turns[-1]["directive"]
    assert "cover this load's requirements from FACTS" in directive
    # Every CONDITION is spoken...
    assert "SAY every CONDITION THEY HAVE TO MEET" in directive
    assert "Never drop or soften one of those" in directive
    # ...and the money terms are not, because none of them is something to agree to.
    assert "DO NOT read out the money terms" in directive
    assert "gladly IF ASKED" in directive
    # Held to a phone length too — this turn measured 33 seconds of speech before
    # the caller was asked to agree to any of it.
    assert "TWO SENTENCES" in directive
    assert agent.state.value == "check_requirements"      # still waiting on a yes
    assert agent._composer.turns[-1]["speakable"] == ""   # and still no rate


def test_a_non_answer_does_not_reach_the_rate(repo):
    """The live failure. "Hello." must not buy a rate quote."""
    agent = _to_requirements(repo)
    agent.handle("go ahead")
    agent.handle("Hello.")

    assert agent.state.value == "check_requirements"
    assert agent._composer.turns[-1]["speakable"] == ""
    assert "did not actually answer" in agent._composer.turns[-1]["directive"]


def test_a_yes_after_the_re_ask_still_works(repo):
    agent = _to_requirements(repo)
    agent.handle("go ahead")
    agent.handle("Hello.")
    agent.handle("yeah, we can do all that")

    assert agent.state.value == "state_price"
    assert agent._composer.turns[-1]["speakable"] == "$1400"


def test_a_caller_who_never_answers_goes_to_a_rep(repo):
    """Not to a rate. A rep can read the requirements to them; the agent has asked
    as many times as is useful."""
    agent = _to_requirements(repo)
    agent.handle("go ahead")
    for _ in range(_MAX_REQUIREMENT_ASKS):
        agent.handle("Hello?")

    assert agent.summary()["outcome"] == "transferred"
    conn = repo._db.connect()
    try:
        notes = " ".join(r["note"] for r in
                         conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()
    assert "never confirmed the requirements" in notes
    assert "rather than treated as agreement" in notes


def test_declining_before_the_requirements_are_read_is_honoured(repo):
    """They have already heard the lane, the dates and the equipment. Making them
    refuse twice — once either side of a list they have decided against — is the
    opposite of listening."""
    agent = _to_requirements(repo)
    agent.handle("no, we can't do that")

    assert agent.summary()["outcome"] == "no_deal"
    conn = repo._db.connect()
    try:
        notes = " ".join(r["note"] for r in
                         conn.execute("SELECT note FROM call_notes").fetchall())
    finally:
        conn.close()
    assert "before the requirements were read out" in notes
