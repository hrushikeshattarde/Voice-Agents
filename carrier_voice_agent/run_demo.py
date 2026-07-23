"""
run_demo.py
-----------
Text-mode simulation of the carrier-sales flow. Uses NO models and NO API keys
-- pure business logic + state machine. Great for verifying the negotiation /
verification / transfer logic before you add voice.

    python run_demo.py            # scripted demo calls
    python run_demo.py --chat     # interactive: you play the carrier
"""

import sys
import database as db
from conversation import CarrierSalesAgent


def scripted(name, turns, max_rounds=6):
    print(f"\n{'='*70}\nSCENARIO: {name}\n{'='*70}")
    db.reset_db()                                # fresh load data each scenario
    agent = CarrierSalesAgent(llm=None, max_rounds=max_rounds)  # template phrasing
    print(f"AGENT : {agent.greeting()}")
    for t in turns:
        print(f"CALLER: {t}")
        print(f"AGENT : {agent.handle(t)}")
        if agent.state == "DONE":
            break
    print(f"--> outcome: {agent.summary()}")


def interactive():
    agent = CarrierSalesAgent(llm=None)
    print(f"AGENT : {agent.greeting()}")
    while agent.state != "DONE":
        try:
            t = input("CALLER: ")
        except EOFError:
            break
        print(f"AGENT : {agent.handle(t)}")
    print("Summary:", agent.summary())


if __name__ == "__main__":
    db.init_db()
    if "--chat" in sys.argv:
        interactive()
    else:
        # L1001: opens 2000, ceiling 2500 (agent may offer up to 2350)
        scripted("Carrier accepts the opening offer", [
            "Hi, I'm calling about load L1001",
            "My MC number is 123456",
            "yeah that works",            # accepts our opening $2000
        ])
        scripted("Agent walks its offer UP by 25-30, carrier then accepts", [
            "L1001",
            "MC 123456",
            "I need 2080 for that",       # >2000 -> agent comes up to 2025
            "come on, 2060",              # -> agent comes up to 2050
            "ok deal",                    # books at agent's $2050 offer
        ])
        # High first ask -> HOLD firm, then WALK UP, then disconnect + note
        # (never hangs up on the first attempt — behaves like a human rep)
        scripted("High ask -> hold firm -> walk up -> disconnect + clear note", [
            "load 1003",
            "MC654321",
            "I need 1500 for this",       # >> opening 900 -> HOLD, restate 900
            "no way, 1500",               # still apart -> walk up to 930
            "come on, 1500",              # -> walk up to 960
            "1500 or nothing",            # patience spent -> disconnect + note
        ], max_rounds=4)
        scripted("Suspiciously cheap -> fraud review, not auto-booked", [
            "L1001",
            "MC123456",
            "I'll haul it for 900",       # < fraud_low 1400 -> human review
        ])
        scripted("Revoked authority -> human review, not hung up", [
            "L1002",
            "MC999888",                   # revoked
        ])
