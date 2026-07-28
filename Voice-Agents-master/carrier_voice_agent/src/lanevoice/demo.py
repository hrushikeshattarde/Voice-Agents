"""
Text-mode simulation of the full call flow — you type, the agent answers.

    lanevoice-demo                    # scripted scenarios, offline seed data
    lanevoice-demo --chat             # interactive, offline seed data
    lanevoice-demo --chat --live      # interactive against REAL Transport Pro
    lanevoice-demo --chat --live --facts    # ...and show the data behind each turn

`--live` is the one to reach for when the question is "is it pulling the right
information". It runs the whole call against the live board — the same repository
the phone worker uses — so a real load number, a real MC and a real email address
go through exactly the path a caller would take. Nothing is written to Transport
Pro until a booking is actually agreed, at which point an offer IS posted; use a
test load if that matters.

`--facts` prints the FACTS block behind every turn: the only load, carrier and
rate values the agent was allowed to speak. That is the fetched data, verbatim,
which is usually what you actually want to see.
"""

from __future__ import annotations

import sys

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.datasource import build_repository
from lanevoice.db import Database, Repository
from lanevoice.env import load_env
from lanevoice.logging_config import setup_logging
from lanevoice.settings import get_settings as _get_settings


def get_settings(live: bool = False):
    """Settings for the demo, pinned to the seed data unless `--live`.

    The scripted scenarios are written against the seeded loads (`L1001`), so
    they always run on SQLite regardless of `DATA_SOURCE` — including the load
    number format the agent listens for. Point those at the live board and every
    scripted turn would be talking about freight that doesn't exist.

    `--live` is the deliberate exception: it honours `DATA_SOURCE` so an
    interactive call can be driven against the real system of record.
    """
    settings = _get_settings()
    return settings if live else settings.model_copy(update={"data_source": "sqlite"})


class _ShowFacts:
    """Wraps a composer and prints the data each turn was built from.

    The agent hands the composer a DIRECTIVE (what the turn must achieve), FACTS
    (everything it is allowed to state) and SPEAKABLE (the only dollar figures it
    may utter). FACTS is the fetched load and carrier data, so printing it is the
    most direct answer to "did it pull the right information" — more direct than
    the sentence that comes out, which is the model's paraphrase of it.
    """

    def __init__(self, inner):
        self._inner = inner

    def compose(self, directive, facts="", dialogue="", speakable="", correction=""):
        # ASCII only. A Windows console is cp1252 by default, and a box-drawing
        # character here raises UnicodeEncodeError inside the composer call — which
        # the agent quite correctly reads as "the composer is broken" and hands the
        # call to a rep. A debugging aid must not be able to end a call.
        if facts:
            print("\n      +-- FACTS the agent may speak this turn " + "-" * 25)
            for line in facts.splitlines():
                print(f"      | {line}")
            print("      +" + "-" * 64)
        if speakable:
            print(f"      $ may say: {speakable}")
        return self._inner.compose(directive=directive, facts=facts,
                                   dialogue=dialogue, speakable=speakable,
                                   correction=correction)

    def read(self, dialogue, fields):
        return self._inner.read(dialogue, fields)


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


def _new_agent(live: bool = False, show_facts: bool = False) -> CarrierSalesAgent:
    settings = get_settings(live)
    composer = _build_composer(settings)
    if show_facts:
        composer = _ShowFacts(composer)

    if live:
        # The same repository the phone worker builds, so this exercises the real
        # lookup path rather than a demo-shaped imitation of it.
        settings.require("transport_pro_url", "transport_pro_username",
                         "transport_pro_password")
        print(f"[data: LIVE Transport Pro at {settings.transport_pro_url}]")
        print(f"[selling loads with status: "
              f"{', '.join(sorted(settings.open_load_statuses)) or '(none)'} "
              f"— and postingInfo.isPosted true]")
        return CarrierSalesAgent(build_repository(settings), composer, settings)

    db = Database(settings.db_path)
    db.reset(seed=True)
    print("[data: offline seed database — load numbers look like L1001]")
    return CarrierSalesAgent(Repository(db), composer, settings)


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


def _interactive(live: bool = False, show_facts: bool = False) -> None:
    agent = _new_agent(live=live, show_facts=show_facts)
    print("[you are the carrier. Ctrl-C or an empty EOF ends the call.]\n")
    print(f"AGENT : {agent.greeting()}")
    while agent.state.value != "done":
        try:
            turn = input("CALLER: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        print(f"AGENT : {agent.handle(turn)}")
    print("\nSummary:", agent.summary())


def main() -> None:
    live = "--live" in sys.argv
    show_facts = "--facts" in sys.argv
    # First, and before any get_settings(): that result is cached, so an .env
    # loaded afterwards would have no effect on this process.
    load_env(verbose=True)
    # Without this, a composer that can't reach its model fails silently and the
    # call just ends up with a rep for no visible reason.
    setup_logging(get_settings(live).log_level)

    # `--live` only makes sense interactively: the scripted scenarios below are
    # written against seeded load numbers that do not exist on a real board.
    if "--chat" in sys.argv or live:
        try:
            _interactive(live=live, show_facts=show_facts)
        except RuntimeError as exc:
            # Almost always missing credentials. A stack trace here tells you
            # nothing you didn't already know and hides the one useful line.
            print(f"\nCannot start: {exc}")
            raise SystemExit(2) from None
        return

    # The address they give is already on Blue Sky's file -> matched, used as is.
    _scripted("Carrier takes the opening offer (+ operational close)", [
        "about L1001", "MC 123456", "empty in Dallas, Texas today",
        "yeah that works", "yep, I can cover it",
        "billing at blue sky logistics dot com",
    ])
    # An address that isn't on their account -> queried once, then handed to a
    # rep. Nothing gets booked and nobody is told they're booked: the booking
    # link only ever goes somewhere already on the carrier's record.
    _scripted("Email not on the carrier's account -> no booking", [
        "L1003", "MC654321", "empty in Phoenix, Arizona right now",
        "that works", "yep can cover it",
        "send it to newdesk at roadrunner freight dot com",
        "no, newdesk at roadrunner freight dot com",
    ])
    # The same call, with an address that IS on their account -> booked.
    _scripted("Email matches the account -> booked, link goes there", [
        "L1003", "MC654321", "empty in Phoenix, Arizona right now",
        "that works", "yep can cover it",
        "send it to ops at roadrunner freight dot com",
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
