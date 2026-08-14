"""
Which LLM writes the agent's turns, and the wire shape each provider expects.

The prompt assembly is the product; the vendor is not. So what these tests pin is
that swapping `LLM_PROVIDER` changes the transport and nothing else — the same
DIRECTIVE, the same FACTS, the same SPEAKABLE guardrail reach whichever model
answers. A provider swap that quietly altered what the agent says would be a
far worse bug than one that failed outright.

Every provider is driven through an `httpx.MockTransport`, so the real SDK code
runs — auth headers, request bodies, response parsing — with no network and no
keys.
"""

import httpx
import pytest

from lanevoice.settings import Settings, get_settings
from lanevoice.voice import (
    AnthropicComposer,
    OpenRouterComposer,
    StubComposer,
    build_composer,
)

DIRECTIVE = "Tell them you're asking $1600 on this one and ask if they want it."
FACTS = "Load number: 2520571\nOrigin: Sikeston, MO"
REPLY = "I've got it at $1600. You want it?"


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


class _Recorder:
    """Captures the outbound request and replies in the provider's own shape."""

    def __init__(self, shape, reply=REPLY):
        self.shape = shape
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.requests.append(request)
        if self.shape == "chat_completions":
            return httpx.Response(200, json={
                "id": "cmpl-1", "object": "chat.completion", "created": 0,
                "model": "test",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": self.reply}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            })
        return httpx.Response(200, json={          # Anthropic messages shape
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": self.reply}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    @property
    def body(self):
        import json
        return json.loads(self.requests[0].content)


# --------------------------------------------------------------------------- #
# Model ids — the naming differs per provider and a carried-over id 404s
# --------------------------------------------------------------------------- #
def test_each_provider_has_its_own_default_model():
    """Both defaults are Claude Sonnet 5, and neither provider accepts the
    other's spelling — so a single shared default would 404 on the first turn."""
    assert (_settings(llm_provider="openrouter").resolved_llm_model
            == "anthropic/claude-sonnet-5")
    assert _settings(llm_provider="anthropic").resolved_llm_model == "claude-sonnet-5"


def test_openrouter_is_the_default_provider():
    """The OpenRouter key is already required for STT and TTS, so composing there
    too is what makes one credential enough to take a call.

    Read off the field default rather than a live `Settings`, so a developer with
    LLM_PROVIDER in their own `.env` doesn't fail the suite.
    """
    assert Settings.model_fields["llm_provider"].default == "openrouter"


def test_model_ids_differ_between_the_gateway_and_the_first_party_api():
    """OpenRouter namespaces by vendor; Anthropic uses a bare id. Neither accepts
    the other's spelling."""
    gateway = _settings(llm_provider="openrouter").resolved_llm_model
    direct = _settings(llm_provider="anthropic").resolved_llm_model
    assert gateway == "anthropic/claude-sonnet-5"
    assert direct == "claude-sonnet-5"
    assert gateway != direct


def test_an_explicit_llm_model_overrides_the_provider_default():
    assert _settings(llm_provider="anthropic",
                     llm_model="claude-haiku-4-5").resolved_llm_model == "claude-haiku-4-5"
    # Whitespace-only is treated as unset, not as a model named " ".
    assert (_settings(llm_provider="anthropic", llm_model="   ").resolved_llm_model
            == "claude-sonnet-5")


def test_the_key_and_its_env_var_name_follow_the_provider():
    """One .env holds both; switching provider must never send an OpenRouter key
    to Anthropic."""
    s = _settings(openrouter_api_key="o", anthropic_api_key="a")
    for provider, key, name in (
        ("openrouter", "o", "OPENROUTER_API_KEY"),
        ("anthropic", "a", "ANTHROPIC_API_KEY"),
    ):
        picked = s.model_copy(update={"llm_provider": provider})
        assert picked.llm_api_key == key
        assert picked.llm_key_name == name


# --------------------------------------------------------------------------- #
# Wire shape per provider
# --------------------------------------------------------------------------- #
def test_openrouter_posts_chat_completions_to_the_gateway():
    rec = _Recorder("chat_completions")
    composer = OpenRouterComposer(_settings(
        llm_provider="openrouter", openrouter_api_key="sk-or-test"))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=rec.transport()))

    assert composer.compose(DIRECTIVE, facts=FACTS, speakable="$1600") == REPLY

    request = rec.requests[0]
    assert "openrouter.ai" in str(request.url)
    assert str(request.url).endswith("/chat/completions")
    assert request.headers["authorization"] == "Bearer sk-or-test"
    assert rec.body["model"] == "anthropic/claude-sonnet-5"
    assert [m["role"] for m in rec.body["messages"]] == ["system", "user"]


def test_anthropic_posts_messages_to_the_first_party_api():
    rec = _Recorder("messages")
    composer = AnthropicComposer(_settings(
        llm_provider="anthropic", anthropic_api_key="sk-ant-test"))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=rec.transport()))

    assert composer.compose(DIRECTIVE, facts=FACTS, speakable="$1600") == REPLY

    request = rec.requests[0]
    assert "api.anthropic.com" in str(request.url)
    assert str(request.url).endswith("/v1/messages")
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert rec.body["model"] == "claude-sonnet-5"
    # The system prompt is a top-level field here, not a message role.
    assert isinstance(rec.body["system"], str)
    assert [m["role"] for m in rec.body["messages"]] == ["user"]


def _recorded_body(model: str, **overrides):
    rec = _Recorder("messages")
    composer = AnthropicComposer(_settings(
        llm_provider="anthropic", anthropic_api_key="k", llm_model=model,
        **overrides))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=rec.transport()))
    composer.compose(DIRECTIVE)
    return rec.body


def test_temperature_is_withheld_from_models_that_reject_it():
    """Sampling parameters were REMOVED with Opus 4.7 and are gone from every
    model after it: a non-default `temperature` is a 400 on Sonnet 5, the default
    composer model. Omitting it is accepted everywhere, so it is omitted.

    The gateway is no protection — OpenRouter drops the parameter today, so
    sending it would break only on LLM_PROVIDER=anthropic, which is exactly the
    path a desk switches to for lower latency.
    """
    for model in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-7",
                  "claude-fable-5", "anthropic/claude-sonnet-5"):
        body = _recorded_body(model, llm_temperature=0.5)
        assert "temperature" not in body, f"{model} must not be sent a temperature"


def test_temperature_still_reaches_the_models_that_take_one():
    """Haiku 4.5 and its generation predate the removal, so a desk that pins one
    of them to save money keeps its configured LLM_TEMPERATURE."""
    for model in ("claude-haiku-4-5", "anthropic/claude-haiku-4.5",
                  "claude-sonnet-4-5", "claude-opus-4-6"):
        body = _recorded_body(model, llm_temperature=0.5)
        assert body["temperature"] == 0.5, f"{model} should keep its temperature"


def test_no_thinking_or_effort_is_sent():
    """This composes one short sentence while a carrier waits on the line, so
    latency is the whole budget. Sonnet 5 supports both `thinking` and `effort` —
    leaving them off is a choice, and turning either on would spend exactly the
    time the caller is sitting in silence."""
    rec = _Recorder("messages")
    composer = AnthropicComposer(_settings(
        llm_provider="anthropic", anthropic_api_key="k"))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=rec.transport()))
    composer.compose(DIRECTIVE)
    assert "thinking" not in rec.body
    assert "output_config" not in rec.body
    assert "effort" not in rec.body


# --------------------------------------------------------------------------- #
# The prompt is identical whoever answers
# --------------------------------------------------------------------------- #
def test_every_provider_receives_the_same_prompt():
    """The guardrail that money outside SPEAKABLE is forbidden lives in the
    prompt. If a provider swap changed the prompt, it would change what the agent
    is allowed to say — the one thing a transport change must never touch."""
    sent = {}
    for provider, cls, shape in (
        ("openrouter", OpenRouterComposer, "chat_completions"),
        ("anthropic", AnthropicComposer, "messages"),
    ):
        rec = _Recorder(shape)
        composer = cls(_settings(
            llm_provider=provider,
            openrouter_api_key="k", anthropic_api_key="k"))
        composer._client = composer._client.with_options(
            http_client=httpx.Client(transport=rec.transport()))
        composer.compose(DIRECTIVE, facts=FACTS, dialogue="Caller: got anything?",
                         speakable="$1600")
        body = rec.body
        system = body["system"] if provider == "anthropic" else \
            body["messages"][0]["content"]
        user = body["messages"][-1]["content"]
        sent[provider] = (system, user)

    assert len({s for s, _ in sent.values()}) == 1, "system prompt diverged"
    assert len({u for _, u in sent.values()}) == 1, "user prompt diverged"

    _, user = sent["anthropic"]
    assert DIRECTIVE in user
    assert FACTS in user
    assert "SPEAKABLE dollar figures: $1600" in user


def test_a_refusal_reads_as_no_turn_rather_than_crashing():
    """A safety decline arrives as HTTP 200 with an empty content list. Returning
    "" is what the conversation layer already knows how to handle — it re-prompts,
    then hands the call to a rep. Indexing content[0] would raise instead."""
    def refuse(_request):
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-haiku-4-5", "content": [],
            "stop_reason": "refusal", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        })

    composer = AnthropicComposer(_settings(
        llm_provider="anthropic", anthropic_api_key="k"))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(refuse)))
    assert composer.compose(DIRECTIVE) == ""


def test_an_openrouter_upstream_error_raises_instead_of_indexing_nothing():
    """OpenRouter reports an upstream failure as a body field on a 200. The
    composer must raise so `_say` retries or hands over — not AttributeError."""
    def upstream_error(_request):
        return httpx.Response(200, json={
            "id": "gen-1", "object": "chat.completion", "created": 0,
            "model": "anthropic/claude-haiku-4.5",
            "error": {"code": 502, "message": "upstream provider error"},
        })

    composer = OpenRouterComposer(_settings(
        llm_provider="openrouter", openrouter_api_key="k"))
    composer._client = composer._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(upstream_error)))
    with pytest.raises(RuntimeError, match="no completion"):
        composer.compose(DIRECTIVE)


# --------------------------------------------------------------------------- #
# The factory
# --------------------------------------------------------------------------- #
def test_the_factory_honours_the_provider_setting():
    keys = {"openrouter_api_key": "k", "anthropic_api_key": "k"}
    for provider, expected in (
        ("openrouter", OpenRouterComposer),
        ("anthropic", AnthropicComposer),
        ("OpenRouter", OpenRouterComposer),      # case-insensitive
        (" anthropic ", AnthropicComposer),      # tolerant of stray whitespace
    ):
        built = build_composer(_settings(llm_provider=provider, use_llm=True, **keys))
        assert isinstance(built, expected), provider


def test_groq_is_no_longer_a_provider():
    """Groq was removed outright rather than left as an alias. A stale
    LLM_PROVIDER=groq in somebody's .env has to stop the worker at startup — the
    alternative is a call that reaches a vendor with no key and no account."""
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        build_composer(_settings(llm_provider="groq", use_llm=True,
                                 openrouter_api_key="k"))


def test_a_typo_in_the_provider_fails_loudly():
    """Falling back to the default on a typo would surface as "why is it billing
    the wrong account?" three calls later, not as an error at startup."""
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        build_composer(_settings(llm_provider="opnrouter", use_llm=True,
                                 openrouter_api_key="k"))


def test_a_missing_key_for_the_chosen_provider_falls_back_to_the_stub():
    """An OpenRouter key set while LLM_PROVIDER=anthropic is not a usable config
    for the composer: the stub makes that visible instead of silently composing on
    the wrong vendor. (Speech still needs the OpenRouter key either way.)"""
    built = build_composer(_settings(
        llm_provider="anthropic", use_llm=True,
        openrouter_api_key="k", anthropic_api_key=""))
    assert isinstance(built, StubComposer)


def test_use_llm_false_always_gives_the_stub():
    built = build_composer(_settings(
        llm_provider="anthropic", use_llm=False, anthropic_api_key="k"))
    assert isinstance(built, StubComposer)
