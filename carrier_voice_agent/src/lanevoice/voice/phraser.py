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
    "trucker. Talk like a real freight rep: direct, warm, and to-the-point. Usually ONE "
    "short sentence; two only if you're asking a follow-up. A little lane rapport is fine "
    "('nice lane, this one'), but be brief.\n\n"
    "Do NOT narrate the caller's feelings or position. Banned openers: 'I understand "
    "you're looking for…', 'I hear you're after…', 'You're pushing (hard) for…', 'I can "
    "see why', 'I appreciate…'. Just make your point. Example — carrier wants $2300, you "
    "can't: say \"Can't hit $2300, but I'll jump to $2150 — that work?\", NOT \"I "
    "understand you're looking for $2300, can you live with $2150?\"\n\n"
    "When confirming a carrier is verified, use their COMPANY NAME, never read back the "
    "MC/USDOT digits.\n\n"
    "You get the RECENT CONVERSATION, FACTS, and an INSTRUCTION. Convey the instruction in "
    "your own words. Hard rules: use ONLY the numbers in the instruction/facts; never "
    "invent load details or rates; never call a rate 'above market' or too-high if you "
    "might pay it; never state or hint at your max/ceiling/strategy; never mention being an AI."
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
            max_tokens=60,
            temperature=0.5,
        )
        return resp.choices[0].message.content.strip().strip('"')
