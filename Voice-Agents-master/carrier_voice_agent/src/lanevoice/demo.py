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

    # The address they give is already on Blue Sky's file -> matched, used as is.
    _scripted("Carrier takes the opening offer (+ operational close)", [
        "about L1001", "MC 123456", "yeah that works", "yep, I can cover it",
        "billing at blue sky logistics dot com",
    ])
    # An address we've never seen -> used AND appended to the carrier's file.
    _scripted("Carrier gives a new email -> added to their file", [
        "L1003", "MC654321", "that works", "yep can cover it",
        "send it to newdesk at roadrunner freight dot com",
    ])
    # Reciprocity then the close: the agent holds while the carrier stonewalls,
    # trades halves once they start moving, splits the last gap, and books it.
    _scripted("Carrier grinds down from a high ask -> agent closes the deal", [
        "L1001", "MC 123456", "I need 2500", "still 2500", "2400", "still 2400",
        "2300", "2200", "2200, that's it", "yep, I can cover it",
        "just use the one you have on file",
    ])
    # Inside Max Buy but above what the bot spends on its own -> a human closes it.
    _scripted("Carrier immovable above the agent's authority -> handed to a rep", [
        "L1001", "MC 123456", "2400", "still 2400", "2400 or I'm gone", "2400",
    ])
    _scripted("Ask above Max Buy -> hold firm -> best and final -> walk away with a note", [
        "load 1003", "MC654321", "I need 1500", "no way 1500",
        "come on 1500", "still 1500", "1500 or nothing",
    ], max_rounds=4)
    _scripted("Suspiciously cheap -> fraud review", [
        "L1001", "MC123456", "I'll haul it for 900",
    ])
    _scripted("Revoked authority -> human review", [
        "L1002", "MC999888",
    ])
    _scripted("Unposted load -> won't proceed", [
        "L1005",
    ])
    _scripted("Carrier not approved to work with us -> declined", [
        "L1001", "MC 222333",
    ])
    _scripted("Load has requirements -> carrier can do it -> books", [
        "L1002", "MC 123456", "yeah I can run it that cold",
        "that works", "yep can make the 8 AM", "just use the one you have on file",
    ])
    _scripted("Load has requirements -> carrier can't -> no booking", [
        "L1002", "MC 123456", "no, I can't run it that cold",
    ])


if __name__ == "__main__":
    main()
