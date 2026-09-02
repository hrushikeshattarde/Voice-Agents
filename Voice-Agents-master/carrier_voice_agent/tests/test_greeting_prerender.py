"""The greeting is composed once per worker process and rendered before the phone rings.

Before this the caller heard nothing until an LLM round trip AND a synthesis had
finished — four to five seconds after pickup, on every call, for a line that is
the same on every call. `compose_greeting` writes that line with nobody waiting;
`greet_with` records it as the call's first turn. What is pinned here is that the
two paths agree: the pre-composed greeting is built from the same directive, the
same facts and the same money guardrail as the live one, and a call opened with
it proceeds exactly like a call opened live.
"""

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.conversation.agent import (
    GREETING_DIRECTIVE,
    GREETING_FACTS,
    CallState,
    _speakable,
    compose_greeting,
)
from lanevoice.settings import get_settings
from lanevoice.voice import StubComposer


class _Composer:
    """Answers every turn with one fixed line, or raises — and counts the calls."""

    def __init__(self, line: str | None = None, error: Exception | None = None):
        self.line, self.error, self.calls = line, error, 0

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.calls += 1
        if self.error:
            raise self.error
        return self.line

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


def test_pre_composed_greeting_uses_the_live_directive_and_facts():
    composer = StubComposer()
    spoken = compose_greeting(composer, get_settings())
    assert spoken
    (turn,) = composer.turns
    assert turn["directive"] == GREETING_DIRECTIVE
    assert turn["facts"] == GREETING_FACTS
    assert turn["speakable"] == _speakable(set())      # no money on the opening line


def test_live_greeting_is_composed_from_the_same_directive(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    agent.greeting()
    (turn,) = agent._composer.turns
    assert turn["directive"] == GREETING_DIRECTIVE
    assert GREETING_FACTS in turn["facts"]


def test_greeting_that_names_money_is_rejected_then_given_up_on():
    composer = _Composer(line="Circle Logistics, Alex speaking, I can do $2400 on that.")
    settings = get_settings().model_copy(update={"llm_attempts": 2})
    assert compose_greeting(composer, settings) is None
    assert composer.calls == 2           # re-prompted with the breach named, then given up


def test_composer_that_is_down_costs_the_prerender_not_the_worker():
    composer = _Composer(error=RuntimeError("HTTP 401 unauthorized"))
    assert compose_greeting(composer, get_settings()) is None
    assert composer.calls == 1           # an auth failure will fail identically; not retried


def test_greet_with_records_the_line_and_the_call_proceeds(repo):
    agent = CarrierSalesAgent(repo, StubComposer(), settings=get_settings())
    line = "Circle Logistics, this is Alex, what can I help you with?"
    assert agent.greet_with(line) == line
    assert agent.state == CallState.IDENTIFY_LOAD
    assert agent.transcript == [("agent", line)]
    assert agent._composer.turns == []   # nothing was composed on the call

    agent.handle("calling about L1001")
    assert agent.state != CallState.IDENTIFY_LOAD
    # The opening line is part of the dialogue every later turn is composed against.
    assert agent.transcript[0] == ("agent", line)
    assert line in agent._dialogue()
