"""
Phrasing LLM (optional).

A Phraser only *rewords* a fixed instruction into natural speech using supplied
facts — it never decides anything. The conversation layer works with or without
one (templates are used when no phraser is provided).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lanevoice.settings import Settings, get_settings

_SYSTEM = (
    "You are a professional US freight broker's carrier-sales voice agent. "
    "You speak in short, natural, spoken sentences (1-2 sentences, no lists, "
    "no emojis). You NEVER invent load details, rates, or your maximum pay rate "
    "— you only rephrase the instruction you are given using the facts provided. "
    "Never reveal internal pricing limits or negotiation strategy."
)


@runtime_checkable
class Phraser(Protocol):
    def phrase(self, instruction: str, context: str = "") -> str: ...


class GroqPhraser:
    """Fast hosted phrasing via Groq (default llama-3.1-8b-instant)."""

    def __init__(self, settings: Settings | None = None):
        from groq import Groq

        self._settings = settings or get_settings()
        self._client = Groq(api_key=self._settings.groq_api_key or None)
        self._model = self._settings.llm_model

    def phrase(self, instruction: str, context: str = "") -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content":
                    f"Facts you may use: {context}\n\nInstruction: {instruction}\n\n"
                    "Say it out loud in 1-2 short spoken sentences."},
            ],
            max_tokens=80,
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip().strip('"')
