"""The voice starts on the reply's first sentence, not its last.

Composing measured 2.7-5.0s per turn on the shipped model and was the whole
of the gap the caller sat through. Now the composer yields text as the model
writes it, the brain checks each finished sentence against the money guard and
hands it to the worker, and the worker feeds it to one utterance while the rest
is still being written. What these pin:

* The guard is not weakened: a sentence that names money it was not given
  never reaches the voice; what was already heard stands and the retry is told
  to continue from there rather than start over.
* A turn that has to state a figure (`must_say`) is composed whole, as before.
* A reply cut off at the token limit keeps its whole sentences and drops the
  fragment; one with nothing whole falls back to the path that asks for a
  shorter reply.
* Both providers stream over the wire in their own shape, driven through the
  real SDKs against a mock transport.
* The worker's bridge turns chunks from another thread into one `say()`.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.settings import get_settings
from lanevoice.telephony import worker
from lanevoice.voice import AnthropicComposer, OpenRouterComposer, StubComposer


class _Streamer:
    """A composer whose `compose_stream` plays scripted attempts, delta by delta."""

    def __init__(self, attempts: list[tuple[list[str], bool]], whole: str = "Short version."):
        self.attempts = list(attempts)
        self.whole = whole
        self.calls: list[dict] = []
        self.whole_calls: list[dict] = []
        self.last_truncated = False
        self.turns: list[dict] = []

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        self.whole_calls.append({"directive": directive, "correction": correction})
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        return self.whole

    def compose_stream(self, directive, facts="", dialogue="", speakable="", correction="",
                       already_said=""):
        self.calls.append({"correction": correction, "already_said": already_said,
                           "speakable": speakable})
        self.turns.append({"directive": directive, "facts": facts,
                           "speakable": speakable, "correction": correction})
        deltas, truncated = self.attempts.pop(0)
        self.last_truncated = False
        yield from deltas
        self.last_truncated = truncated

    def read(self, dialogue, fields):
        return dict.fromkeys(fields)


class _Sink:
    def __init__(self):
        self.events: list[tuple[str, str | None]] = []

    def begin(self):
        self.events.append(("begin", None))

    def chunk(self, text):
        self.events.append(("chunk", text))

    def end(self):
        self.events.append(("end", None))

    @property
    def spoken(self) -> list[str]:
        return [t for kind, t in self.events if kind == "chunk"]


def _brain(repo, composer):
    a = CarrierSalesAgent(repo, composer, settings=get_settings())
    a.greet_with("Circle Logistics, this is Alex.")
    return a


DIRECTIVE = "Ask for their MC number."


# --------------------------------------------------------------------------- #
# The brain streams a turn sentence by sentence
# --------------------------------------------------------------------------- #
def test_sentences_reach_the_voice_as_they_finish_and_the_tail_at_the_end(repo):
    composer = _Streamer([(["Got it, load 25", "32717. That the one? ",
                            "Alright, MC", " number please"], False)])
    a = _brain(repo, composer)
    sink = _Sink()
    a.speech_sink = sink
    spoken = a._say(DIRECTIVE, facts="Load number: 2532717", amounts=set())
    assert sink.spoken == ["Got it, load 2532717.", "That the one?", "Alright, MC number please"]
    assert spoken == "Got it, load 2532717. That the one? Alright, MC number please"
    assert a.transcript[-1] == ("agent", spoken)
    assert sink.events[0] == ("begin", None) and sink.events[-1] == ("end", None)
    assert composer.whole_calls == []                    # never composed whole


def test_without_a_sink_the_turn_is_composed_whole_as_before(repo):
    composer = _Streamer([])
    a = _brain(repo, composer)
    assert a._say(DIRECTIVE, amounts=set()) == "Short version."
    assert composer.calls == [] and len(composer.whole_calls) == 1


def test_a_turn_that_must_state_a_figure_is_composed_whole(repo):
    composer = _Streamer([], whole="I'm at $1400 on it.")
    a = _brain(repo, composer)
    a.speech_sink = _Sink()
    assert a._say("Hold at $1400.", amounts={1400}, must_say=1400) == "I'm at $1400 on it."
    assert composer.calls == []                          # the guard needs the whole reply
    assert a.speech_sink.events == []


def test_a_composer_that_cannot_stream_is_used_whole(repo):
    a = _brain(repo, StubComposer())
    a.speech_sink = _Sink()
    spoken = a._say(DIRECTIVE, amounts=set())
    # The stub CAN stream (one piece), so it goes through the sink...
    assert a.speech_sink.spoken == [spoken]

    class _Whole:
        def compose(self, directive, **_kw):
            return "Whole only."

        def read(self, dialogue, fields):
            return dict.fromkeys(fields)

    b = _brain(repo, _Whole())
    b.speech_sink = _Sink()
    assert b._say(DIRECTIVE, amounts=set()) == "Whole only."   # ...one without it does not
    assert b.speech_sink.events == []


def test_money_the_turn_was_not_given_never_reaches_the_voice(repo):
    composer = _Streamer([
        (["Sure thing. ", "I can do $1800 for you. ", "Sound good?"], False),
        (["Let me check on the rate for you. ", "One second."], False),
    ])
    a = _brain(repo, composer)
    sink = _Sink()
    a.speech_sink = sink
    spoken = a._say(DIRECTIVE, amounts=set())
    # Sentence one was heard before the breach; the retry carried on from it.
    assert sink.spoken == ["Sure thing.", "Let me check on the rate for you.", "One second."]
    assert "$1800" not in " ".join(sink.spoken)
    assert spoken == "Sure thing. Let me check on the rate for you. One second."
    assert composer.calls[1]["already_said"] == "Sure thing."
    assert "money you were not given" in composer.calls[1]["correction"]
    assert a.transcript[-1] == ("agent", spoken)


def test_a_reply_cut_off_at_the_token_limit_keeps_its_whole_sentences(repo):
    composer = _Streamer([(["First sentence. ", "Second sentence. ", "Third sen"], True)])
    a = _brain(repo, composer)
    sink = _Sink()
    a.speech_sink = sink
    assert a._say(DIRECTIVE, amounts=set()) == "First sentence. Second sentence."
    assert sink.spoken == ["First sentence.", "Second sentence."]
    assert len(composer.calls) == 1                      # no second attempt for a fragment


def test_a_reply_with_nothing_whole_falls_back_to_the_shorter_retry(repo):
    composer = _Streamer([(["One long sentence that never gets to its"], True)],
                         whole="Short version.")
    a = _brain(repo, composer)
    sink = _Sink()
    a.speech_sink = sink
    assert a._say(DIRECTIVE, amounts=set()) == "Short version."
    assert sink.spoken == ["Short version."]
    assert len(composer.whole_calls) == 1


def test_an_empty_stream_is_retried_with_a_correction(repo):
    composer = _Streamer([([], False), (["Say it again please."], False)])
    a = _brain(repo, composer)
    sink = _Sink()
    a.speech_sink = sink
    assert a._say(DIRECTIVE, amounts=set()) == "Say it again please."
    assert "returned nothing" in composer.calls[1]["correction"]


def test_every_attempt_failing_hands_the_call_over_after_what_was_heard(repo):
    settings = get_settings().model_copy(update={"llm_attempts": 2})
    composer = _Streamer([
        (["Alright. ", "That'll be $9999. "], False),
        (["Call it $8888 then."], False),
    ])
    a = CarrierSalesAgent(repo, composer, settings=settings)
    a.greet_with("Circle Logistics, this is Alex.")
    sink = _Sink()
    a.speech_sink = sink
    spoken = a._say(DIRECTIVE, amounts=set())
    assert sink.spoken[0] == "Alright."                  # heard before the breach
    assert "$9999" not in " ".join(sink.spoken) and "$8888" not in " ".join(sink.spoken)
    assert a.state.value == "done"                       # handed to a rep
    assert spoken.startswith("Alright. ")
    # What was heard, then the handoff line — both on the record.
    assert [who for who, _ in a.transcript[-2:]] == ["agent", "agent"]


# --------------------------------------------------------------------------- #
# The providers stream over the wire
# --------------------------------------------------------------------------- #
def _event_stream(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=body.encode())


def _chat_chunks(*chunks: dict) -> str:
    base = {"id": "gen-1", "object": "chat.completion.chunk", "created": 0, "model": "m"}
    lines = [f"data: {json.dumps({**base, **chunk})}\n\n" for chunk in chunks]
    return "".join(lines) + "data: [DONE]\n\n"


def _delta(content, finish=None, **extra) -> dict:
    return {"choices": [{"index": 0, "delta": content, "finish_reason": finish}], **extra}


def test_openrouter_streams_chat_completion_chunks():
    requests: list[httpx.Request] = []

    def handle(request):
        requests.append(request)
        return _event_stream(_chat_chunks(
            _delta({"role": "assistant", "content": "I've got it at "}),
            _delta({"content": "$1600. You want it?"}),
            _delta({}, finish="stop",
                   usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}),
        ))

    composer = OpenRouterComposer(get_settings().model_copy(update={
        "llm_provider": "openrouter", "openrouter_api_key": "k"}))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    deltas = list(composer.compose_stream("Quote $1600.", speakable="$1600"))
    assert "".join(deltas) == "I've got it at $1600. You want it?"
    assert composer.last_truncated is False
    assert composer._last_usage == (12, 7)
    assert json.loads(requests[0].content)["stream"] is True


def test_openrouter_reports_a_reply_cut_off_at_the_limit():
    def handle(_request):
        return _event_stream(_chat_chunks(
            _delta({"content": "This runs on and"}),
            _delta({}, finish="length"),
        ))

    composer = OpenRouterComposer(get_settings().model_copy(update={
        "llm_provider": "openrouter", "openrouter_api_key": "k"}))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert "".join(composer.compose_stream("Say a lot.")) == "This runs on and"
    assert composer.last_truncated is True


def _anthropic_events(*texts: str, stop: str = "end_turn") -> str:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
    ]
    for text in texts:
        events.append(("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text}}))
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop, "stop_sequence": None},
                           "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def test_anthropic_streams_message_events():
    requests: list[httpx.Request] = []

    def handle(request):
        requests.append(request)
        return _event_stream(_anthropic_events("I've got it at ", "$1600. You want it?"))

    composer = AnthropicComposer(get_settings().model_copy(update={
        "llm_provider": "anthropic", "anthropic_api_key": "k"}))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    deltas = list(composer.compose_stream("Quote $1600.", speakable="$1600"))
    assert "".join(deltas) == "I've got it at $1600. You want it?"
    assert composer.last_truncated is False
    assert composer._last_usage == (12, 7)
    assert json.loads(requests[0].content)["stream"] is True


def test_anthropic_reports_a_reply_cut_off_at_the_limit():
    composer = AnthropicComposer(get_settings().model_copy(update={
        "llm_provider": "anthropic", "anthropic_api_key": "k"}))
    composer._client = composer._client.with_options(http_client=httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: _event_stream(_anthropic_events("This runs on", stop="max_tokens")))))
    assert "".join(composer.compose_stream("Say a lot.")) == "This runs on"
    assert composer.last_truncated is True


def test_the_continuation_prompt_tells_the_model_what_was_already_heard():
    prompt = OpenRouterComposer._prompt("Do X.", "F", "D", "", "you said money", "Sure thing.")
    assert 'ALREADY SAID THIS, and the caller heard it: "Sure thing."' in prompt
    assert "YOUR LAST ATTEMPT WAS REJECTED: you said money" in prompt
    assert "ALREADY SAID" not in OpenRouterComposer._prompt("Do X.", "F", "D", "", "")


# --------------------------------------------------------------------------- #
# The worker's bridge: chunks from another thread become one utterance
# --------------------------------------------------------------------------- #
class _Speech:
    def __init__(self, text):
        self.text = text
        self.streamed: list[str] = []
        self.done = asyncio.get_running_loop().create_future()
        if isinstance(text, str):
            self.done.set_result(None)
        else:
            asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self):
        async for piece in self.text:
            self.streamed.append(piece)
        self.done.set_result(None)

    def __await__(self):
        return self.done.__await__()


class _Session:
    def __init__(self):
        self.handles: list[_Speech] = []

    def say(self, text, **_kw):
        handle = _Speech(text)
        self.handles.append(handle)
        return handle


def test_streamed_sentences_become_one_utterance():
    async def run():
        session = _Session()
        turn = worker._TurnSpeech(session, asyncio.get_running_loop())
        pump = asyncio.create_task(turn.pump())
        # The brain speaks from its own thread.
        await asyncio.to_thread(lambda: (turn.begin(), turn.chunk("Got it."),
                                         turn.chunk("That the one?"), turn.end()))
        assert await asyncio.wait_for(turn.first_text, 1) is True
        turn.finished()
        await pump
        assert len(session.handles) == 1
        await session.handles[0]
        return session.handles[0].streamed, turn.sentences

    streamed, count = asyncio.run(run())
    assert streamed == ["Got it. ", "That the one? "]
    assert count == 2


def test_a_turn_that_never_streams_resolves_first_text_false_and_says_nothing():
    async def run():
        session = _Session()
        turn = worker._TurnSpeech(session, asyncio.get_running_loop())
        pump = asyncio.create_task(turn.pump())
        turn.finished()
        await pump
        return await turn.first_text, session.handles

    first, handles = asyncio.run(run())
    assert first is False and handles == []


def test_two_lines_in_one_turn_are_two_utterances():
    async def run():
        session = _Session()
        turn = worker._TurnSpeech(session, asyncio.get_running_loop())
        pump = asyncio.create_task(turn.pump())
        for line in ("First line.", "Second line."):
            turn.begin()
            turn.chunk(line)
            turn.end()
        await asyncio.sleep(0)
        turn.finished()
        await pump
        for handle in session.handles:
            await handle
        return [handle.streamed for handle in session.handles]

    assert asyncio.run(run()) == [["First line. "], ["Second line. "]]


def test_the_switch_defaults_on():
    assert get_settings().stream_compose is True
    assert get_settings().model_copy(update={"stream_compose": False}).stream_compose is False
