"""
Text-mode simulation of the full call flow — no API keys, no models.

    lanevoice-demo            # scripted scenarios
    lanevoice-demo --chat     # interactive: you play the carrier
"""

from __future__ import annotations

import sys

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.db import Database, Repository
from lanevoice.settings import get_settings


def _build_phraser(settings):
    """Use the Groq persona LLM when a key is configured (natural, human wording)."""
    if not settings.groq_api_key:
        return None
    try:
        from lanevoice.voice import GroqPhraser
        return GroqPhraser(settings)
    except Exception:  # noqa: BLE001
        return None


def _new_agent(reset: bool = True) -> CarrierSalesAgent:
    settings = get_settings()
    db = Database(settings.db_path)
    if reset:
        db.reset(seed=True)
    else:
        db.init(seed=True)
    return CarrierSalesAgent(Repository(db), _build_phraser(settings), settings)


def _scripted(name: str, turns: list[str], max_rounds: int | None = None) -> None:
    print(f"\n{'=' * 70}\nSCENARIO: {name}\n{'=' * 70}")
    settings = get_settings()
    db = Database(settings.db_path)
    db.reset(seed=True)
    if max_rounds is not None:
        settings = settings.model_copy(update={"max_negotiation_rounds": max_rounds})
    agent = CarrierSalesAgent(Repository(db), phraser=None, settings=settings)
    print(f"AGENT : {agent.greeting()}")
    for turn in turns:
        print(f"CALLER: {turn}")
        print(f"AGENT : {agent.handle(turn)}")
        if agent.state.value == "done":
            break
    print(f"--> {agent.summary()}")


def _interactive() -> None:
    agent = _new_agent(reset=True)
    print(f"AGENT : {agent.greeting()}")
    while agent.state.value != "done":
        try:
            turn = input("CALLER: ")
        except EOFError:
            break
        print(f"AGENT : {agent.handle(turn)}")
    print("Summary:", agent.summary())


def main() -> None:
    if "--chat" in sys.argv:
        _interactive()
        return

    _scripted("Carrier takes the opening offer (+ operational close)", [
        "about L1001", "MC 123456", "yeah that works", "yep, I can cover it",
    ])
    _scripted("Carrier holds high; agent splits the difference, then books", [
        "L1001", "MC 123456", "I need 2300", "2300", "2300", "2300",
        "yep can make the pickup",
    ])
    _scripted("High ask -> hold firm -> concede -> walk away with a note", [
        "load 1003", "MC654321", "I need 1500", "no way 1500",
        "come on 1500", "1500 or nothing",
    ], max_rounds=4)
    _scripted("Suspiciously cheap -> fraud review", [
        "L1001", "MC123456", "I'll haul it for 900",
    ])
    _scripted("Revoked authority -> human review", [
        "L1002", "MC999888",
    ])


if __name__ == "__main__":
    main()
