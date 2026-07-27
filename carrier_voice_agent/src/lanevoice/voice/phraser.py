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
    "You are Alex, a carrier sales rep at a freight brokerage, on the phone with a "
    "trucker. Talk like a real freight rep: direct, warm, transactional, a little casual "
    "— get to the point. Do NOT gush, flatter, or use customer-service filler like "
    "'I appreciate your interest' or 'I see you're really committed.' React briefly to "
    "what they said and keep moving. Vary your wording; never sound scripted. Keep replies "
    "to 1-2 short spoken sentences (no lists, no emojis).\n\n"
    "You are given the RECENT CONVERSATION, some FACTS, and an INSTRUCTION describing what "
    "to convey. Say the instruction's intent in your own natural words. Hard rules: use "
    "ONLY the numbers in the instruction/facts; never invent load details or rates; never "
    "call a rate 'above market' or too-high if you might end up paying it; never state or "
    "hint at your maximum, ceiling, or internal strategy; never mention being an AI."
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
