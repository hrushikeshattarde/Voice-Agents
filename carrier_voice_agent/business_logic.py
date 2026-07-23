"""
business_logic.py
-----------------
The deterministic "product" layer (PRD §4).

CORE SECURITY PRINCIPLE (PRD §4 / §9.4):
    The LLM is the conversational interface ONLY. It never decides whether an
    MC number is valid, and it never accepts a price. Every consequential
    decision is plain, unit-testable Python here. A caller cannot talk the
    model into a bad outcome because the model has no power to cause one.

Functions here are pure-ish (they read/write the DB) and return structured
dicts that the conversation layer turns into speech.
"""

import re
import database as db


# --------------------------------------------------------------------------- #
# Entity extraction from a spoken utterance (regex first — cheap & reliable)
# --------------------------------------------------------------------------- #
def extract_load_id(text: str):
    """Match things like 'L1001', 'load 1001', 'L 10 01'."""
    t = text.upper().replace(" ", "")
    m = re.search(r"L?\d{4,6}", t)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group())
    return f"L{digits}" if not m.group().startswith("L") else m.group()


def extract_mc_dot(text: str):
    """Return ('MC'|'DOT', number_str) or (None, None)."""
    t = text.upper()
    m = re.search(r"(MC|DOT|USDOT)[\s#:-]*(\d{4,8})", t)
    if m:
        kind = "DOT" if m.group(1) != "MC" else "MC"
        return kind, m.group(2)
    # bare number fallback
    m = re.search(r"\b(\d{6,8})\b", t)
    if m:
        return "DOT", m.group(1)
    return None, None


def extract_money(text: str):
    """Extract a dollar amount. Handles '$2,100', '2100', '21 hundred', '2.1k'."""
    t = text.lower().replace(",", "")
    m = re.search(r"\$?\s*(\d{3,6})(?:\s*(?:dollars|bucks))?", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*k", t)
    if m:
        return float(m.group(1)) * 1000
    return None


# --------------------------------------------------------------------------- #
# Step 2 — Load lookup
# --------------------------------------------------------------------------- #
def lookup_load(load_id: str) -> dict:
    load = db.get_load(load_id)
    if not load:
        return {"found": False}
    return {
        "found": True,
        "available": load["status"] == "open",
        "load": load,
    }


# --------------------------------------------------------------------------- #
# Step 3 — Carrier verification  (mock FMCSA / PRD §8)
# --------------------------------------------------------------------------- #
def verify_carrier(mc_or_dot: str) -> dict:
    """
    Deterministic verification with an explicit fraud-signal rule layer
    (PRD §8.2). In production, the DB lookup here is replaced by a call to
    FMCSA QCMobile + a commercial fallback, but the decision logic stays.
    """
    carrier = db.get_carrier(mc_or_dot)
    if not carrier:
        return {"verified": False, "reason": "not_found",
                "action": "human_review"}

    risk_flags = []
    if carrier["authority_status"] != "active":
        risk_flags.append(f"authority_{carrier['authority_status']}")
    if not carrier["insurance_on_file"]:
        risk_flags.append("insurance_lapse")
    if carrier["authority_reactivated_days"] is not None and \
            carrier["authority_reactivated_days"] <= 90:
        risk_flags.append("recently_reactivated")

    hard_fail = carrier["authority_status"] != "active" or not carrier["insurance_on_file"]

    return {
        "verified": not hard_fail,
        "high_risk": bool(risk_flags),
        "risk_flags": risk_flags,
        "carrier": carrier,
        # PRD §3 step 3: fraud attempts are logged & routed to review, never dropped
        "action": "proceed" if not hard_fail and not risk_flags
                  else "human_review",
    }


# --------------------------------------------------------------------------- #
# Step 5 — Negotiation engine  (THE hard server-side check — PRD §9.4)
# --------------------------------------------------------------------------- #
class Negotiation:
    """
    Deterministic negotiation state for one call/load.

    Strategy (as specified by the desk):
      * The agent OPENS at `open_rate` (a low starting offer).
      * If the carrier wants more, the agent walks its offer UP by `step`
        (~25-30, larger when the gap is big).
      * The agent may NEVER offer more than  ceiling - BUFFER  (BUFFER=150).
        That $150 is held back for a human to use on a warm transfer.
      * If the carrier asks ABOVE the ceiling -> NO DEAL: log a note, decline,
        end the call.

    accept/reject/no-deal is decided ONLY here, against the live ceiling — the
    LLM cannot influence it (PRD §9.4).
    """

    BUFFER = 150          # $ held below ceiling; agent may not cross it
    STEP_SMALL = 25       # normal increment
    STEP_BIG = 30         # increment when the carrier is still far away

    def __init__(self, load: dict, max_rounds: int = 6):
        self.load = load
        self.open = load["open_rate"]              # advertised/opening (low) price
        self.ceiling = load["ceiling_rate"]        # true budget max
        self.fraud_low = load["fraud_low_rate"]
        self.agent_max = max(self.open, self.ceiling - self.BUFFER)  # most agent can offer
        self.max_rounds = max_rounds
        self.round = 0
        self.last_agent_offer = self.open          # current offer on the table
        self.held_firm = False                     # have we pushed back once yet?
        self.history = []

    def _step(self, gap: float) -> int:
        return self.STEP_BIG if gap > 100 else self.STEP_SMALL

    def evaluate(self, carrier_ask: float) -> dict:
        """
        `carrier_ask` = the rate the carrier wants to be PAID.

        Behaves like a human rep: on a high ask it HOLDS FIRM once (restates the
        opening), then WALKS UP by 25-30 per round, and only disconnects (with a
        clear note) once it has reached its cap or run out of patience. It never
        hangs up on the first high ask.

        Returns one of:
          {'decision': 'accept',  'rate': X}
          {'decision': 'hold',    'rate': opening}           # restate low price, don't move
          {'decision': 'counter', 'rate': X}                 # walked our offer up
          {'decision': 'review',  'reason': 'suspiciously_low'}
          {'decision': 'no_deal', 'reason': 'stalemate', 'ask': X,
           'final_offer': X, 'within_ceiling': bool}         # disconnect + note
        """
        self.round += 1
        self.history.append(("carrier", carrier_ask))

        # Fraud tripwire: absurdly cheap -> double-brokering / no-show risk.
        if carrier_ask < self.fraud_low:
            return {"decision": "review", "reason": "suspiciously_low"}

        # Carrier is at/below what we're already offering -> book it (cheap for us).
        if carrier_ask <= self.last_agent_offer:
            return {"decision": "accept", "rate": carrier_ask}

        # Carrier wants MORE than our current offer.
        # First push-back: hold firm and restate the opening price (don't move yet).
        if not self.held_firm:
            self.held_firm = True
            return {"decision": "hold", "rate": self.last_agent_offer,
                    "ask": carrier_ask}

        # Already held once -> consider walking our offer UP toward the cap.
        gap = carrier_ask - self.last_agent_offer
        proposed = min(self.last_agent_offer + self._step(gap), self.agent_max)
        moved = proposed > self.last_agent_offer

        # If we still can't reach them and either can't move further (at cap) or
        # patience is spent, walk away at our LAST OFFER ACTUALLY MADE.
        if carrier_ask > proposed and (not moved
                                       or proposed >= self.agent_max
                                       or self.round >= self.max_rounds):
            return {"decision": "no_deal", "reason": "stalemate",
                    "ask": carrier_ask, "final_offer": self.last_agent_offer,
                    "within_ceiling": carrier_ask <= self.ceiling}

        # Make the raised offer.
        self.last_agent_offer = proposed
        self.history.append(("agent", proposed))
        if carrier_ask <= proposed:            # our offer now meets their ask
            return {"decision": "accept", "rate": carrier_ask}
        return {"decision": "counter", "rate": proposed}


# --------------------------------------------------------------------------- #
# Step 6b — Rep lookup + transfer  (PRD §3 / §9.5)
# --------------------------------------------------------------------------- #
def resolve_transfer(load: dict) -> dict:
    rep = db.get_rep(load["assigned_rep_id"]) if load.get("assigned_rep_id") else None
    if rep and rep["available"]:
        return {"transfer_to": rep, "fallback": False}
    # PRD §9.5: never dead-air disconnect — fall back to another available rep
    fallback = db.get_available_rep_fallback(
        exclude_rep_id=load.get("assigned_rep_id"))
    if fallback:
        return {"transfer_to": fallback, "fallback": True}
    return {"transfer_to": None, "fallback": True,
            "note": "voicemail_plus_callback_task"}
