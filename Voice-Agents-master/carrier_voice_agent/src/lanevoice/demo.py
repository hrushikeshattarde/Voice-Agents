"""
Text-mode simulation of the full call flow — no API keys, no models.

    lanevoice-demo            # scripted scenarios
    lanevoice-demo --chat     # interactive: you play the carrier
"""

from __future__ import annotations

import sys

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.db import Database, Repository
from lanevoice.logging_config import setup_logging
from lanevoice.settings import get_settings


def _build_composer(settings):
    """The real composer when one is configured, otherwise the offline stub.

    Without a model the agent cannot speak — it has no scripted lines — so which
    one you got matters a great deal to what you're about to read. Say so plainly
    rather than letting the difference show up as a mystery handoff.
    """
    from lanevoice.voice import StubComposer

    if not settings.use_llm:
        print("[composer: offline stub — USE_LLM=false. Turns below are the agent's "
              "INTENT, not speech.]")
        return StubComposer(settings)
    if not settings.groq_api_key:
        print("[composer: offline stub — no GROQ_API_KEY set. Turns below are the "
              "agent's INTENT, not speech.]")
        return StubComposer(settings)
    try:
        from lanevoice.voice import GroqComposer
        return GroqComposer(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"[composer: offline stub — could not start the Groq composer ({exc}). "
              "Turns below are the agent's INTENT, not speech.]")
        return StubComposer(settings)


def _new_agent(reset: bool = True) -> CarrierSalesAgent:
    settings = get_settings()
    db = Database(settings.db_path)
    if reset:
        db.reset(seed=True)
    else:
        db.init(seed=True)
    return CarrierSalesAgent(Repository(db), _build_composer(settings), settings)


def _scripted(name: str, turns: list[str], max_rounds: int | None = None) -> None:
    print(f"\n{'=' * 70}\nSCENARIO: {name}\n{'=' * 70}")
    settings = get_settings()
    db = Database(settings.db_path)
    db.reset(seed=True)
    if max_rounds is not None:
        settings = settings.model_copy(update={"max_negotiation_rounds": max_rounds})
    agent = CarrierSalesAgent(Repository(db), _build_composer(settings), settings)
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
    # Without this, a composer that can't reach its model fails silently and the
    # call just ends up with a rep for no visible reason.
    setup_logging(get_settings().log_level)
    if "--chat" in sys.argv:
        _interactive()
        return

    # The address they give is already on Blue Sky's file -> matched, used as is.
    _scripted("Carrier takes the opening offer (+ operational close)", [
        "about L1001", "MC 123456", "empty in Dallas, Texas today",
        "yeah that works", "yep, I can cover it",
        "billing at blue sky logistics dot com",
    ])
    # An address we've never seen -> used AND appended to the carrier's file.
    _scripted("Carrier gives a new email -> added to their file", [
        "L1003", "MC654321", "empty in Phoenix, Arizona right now",
        "that works", "yep can cover it",
        "send it to newdesk at roadrunner freight dot com",
    ])
    # Reciprocity then the close: the agent holds while the carrier stonewalls,
    # trades halves once they start moving, splits the last gap, and books it.
    _scripted("Carrier grinds down from a high ask -> agent closes the deal", [
        "L1001", "MC 123456", "empty in Joliet, Illinois tomorrow morning",
        "I need 2500", "still 2500", "2400", "still 2400",
        "2300", "2200", "2200, that's my best", "yep, I can cover it",
        "just use the one you have on file",
    ])
    # Inside Max Buy but above what the bot spends on its own -> a human closes it.
    _scripted("Carrier immovable above the agent's authority -> handed to a rep", [
        "L1001", "MC 123456", "empty in Gary, Indiana today", "2400", "still 2400",
        "2400 or I'm gone", "2400",
    ])
    _scripted("Ask above Max Buy -> hold firm -> best and final -> walk away with a note", [
        "load 1003", "MC654321", "empty in Ontario, California right now",
        "I need 1500", "no way 1500",
        "come on 1500", "still 1500", "1500 or nothing",
    ], max_rounds=4)
    _scripted("Suspiciously cheap -> fraud review", [
        "L1001", "MC123456", "empty in Chicago, Illinois today", "I'll haul it for 900",
    ])
    _scripted("Suspended authority -> blocked before any rate", [
        "L1002", "MC999888",
    ])
    _scripted("Inactive authority -> blocked before any rate", [
        "L1002", "MC 555444",
    ])
    _scripted("Unposted load -> won't proceed", [
        "L1005",
    ])
    _scripted("Carrier not approved to work with us -> declined", [
        "L1001", "MC 222333",
    ])
    _scripted("Load has requirements -> carrier can do it -> books", [
        "L1002", "MC 123456", "empty in Atlanta, Georgia today",
        "yeah I can run it that cold",
        "that works", "yep can make the 8 AM", "just use the one you have on file",
    ])
    _scripted("Load has requirements -> carrier can't -> no booking", [
        "L1002", "MC 123456", "empty in Atlanta, Georgia today",
        "no, I can't run it that cold",
    ])


if __name__ == "__main__":
    main()
