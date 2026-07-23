"""
conversation.py
---------------
The call state machine (PRD §3). This is the agent's "brain", independent of
how audio gets in/out. It drives:

    GREETING -> IDENTIFY_LOAD -> VERIFY_CARRIER -> STATE_PRICE
             -> NEGOTIATE -> RESOLVE (book | transfer | abandon)

The LLM is used ONLY to phrase the reply naturally given the state + the facts
that deterministic code (business_logic.py) has already decided. If no LLM is
provided, it falls back to clean template strings — so the whole flow is
testable with zero models loaded.
"""

import uuid
import business_logic as bl
import database as db

CONSENT_LINE = ("Thanks for calling the load desk. Just so you know, this call "
                "may be recorded for quality purposes. ")


class CarrierSalesAgent:
    def __init__(self, llm=None, max_rounds: int = 6):
        self.llm = llm                 # optional: object with .phrase(instruction, context)
        self.max_rounds = max_rounds
        self.call_id = f"CALL-{uuid.uuid4().hex[:8]}"
        self.state = "GREETING"
        self.load = None
        self.carrier = None
        self.neg = None
        self.transcript = []
        self.outcome = None
        db.start_call(self.call_id)

    # -- phrasing helper ---------------------------------------------------- #
    def _say(self, fallback: str, instruction: str = None, context: str = None):
        """Return agent speech. Use LLM to naturalize if available."""
        text = fallback
        if self.llm is not None and instruction is not None:
            try:
                text = self.llm.phrase(instruction, context or "")
            except Exception:
                text = fallback
        self.transcript.append(("agent", text))
        return text

    def _log_user(self, text: str):
        self.transcript.append(("carrier", text))

    # -- main entry --------------------------------------------------------- #
    def greeting(self) -> str:
        self.state = "IDENTIFY_LOAD"
        return self._say(
            CONSENT_LINE + "Which load are you calling about? "
            "You can give me the load ID or the lane.",
            instruction="Greet a carrier calling about a freight load. Include a "
                        "one-line call-recording disclosure, then ask which load "
                        "they want (load ID or lane). Be brief and professional.",
        )

    def handle(self, user_text: str) -> str:
        """Feed one carrier utterance, get the agent's spoken reply."""
        self._log_user(user_text)
        handler = {
            "IDENTIFY_LOAD": self._identify_load,
            "VERIFY_CARRIER": self._verify_carrier,
            "STATE_PRICE": self._negotiate,   # first offer arrives here
            "NEGOTIATE": self._negotiate,
            "RESOLVE": self._resolve_followup,
            "DONE": lambda t: "This call has ended. Goodbye.",
        }.get(self.state, self._identify_load)
        return handler(user_text)

    # -- Step 2: identify load --------------------------------------------- #
    def _identify_load(self, text: str) -> str:
        load_id = bl.extract_load_id(text)
        if not load_id:
            return self._say(
                "I didn't catch a load ID. Could you repeat it? For example, L1001.",
                instruction="Politely say you didn't catch the load ID and ask them "
                            "to repeat it, giving 'L1001' as an example format.")
        result = bl.lookup_load(load_id)
        if not result["found"]:
            opens = ", ".join(l["load_id"] for l in db.get_open_loads())
            return self._say(
                f"I couldn't find load {load_id}. Open loads right now are {opens}. "
                "Which one would you like?",
                instruction=f"Tell the caller load {load_id} was not found, then offer "
                            f"the open loads: {opens}. Ask which they want.")
        if not result["available"]:
            opens = ", ".join(l["load_id"] for l in db.get_open_loads())
            return self._say(
                f"Sorry, load {load_id} is already covered. Other open loads: {opens}.",
                instruction=f"Tell the caller load {load_id} is already covered and "
                            f"offer open loads: {opens}.")

        self.load = result["load"]
        self.state = "VERIFY_CARRIER"
        l = self.load
        return self._say(
            f"Got it — load {l['load_id']}, {l['origin']} to {l['destination']}, "
            f"picking up {l['pickup_date']}, {l['equipment']}. "
            "To move forward I'll need your MC or USDOT number.",
            instruction="Confirm the load back to the carrier, then ask for their MC "
                        "or USDOT number.",
            context=f"Load: {l['load_id']} {l['origin']}->{l['destination']} "
                    f"pickup {l['pickup_date']} equip {l['equipment']}")

    # -- Step 3: verify carrier -------------------------------------------- #
    def _verify_carrier(self, text: str) -> str:
        kind, number = bl.extract_mc_dot(text)
        if not number:
            return self._say(
                "I didn't get that. Please say your MC or USDOT number slowly.",
                instruction="Say you didn't catch the number and ask them to repeat "
                            "their MC or USDOT number slowly.")
        v = bl.verify_carrier(number)
        if not v["verified"]:
            self.state = "DONE"
            self.outcome = "rejected"
            self._finish("rejected")
            reason = v.get("reason", "risk flags")
            return self._say(
                "I'm not able to verify active authority and insurance on that number, "
                "so I can't discuss rate right now. I'm routing this to our team for "
                "review and someone will follow up. Thanks for calling.",
                instruction="Firmly but politely explain you cannot verify active "
                            "authority/insurance so you cannot discuss rate, and that "
                            "it is being routed to a human for review. Do not reveal "
                            "internal fraud logic.")
        if v["high_risk"]:
            # verified but flagged -> human review, still logged (never dropped)
            self.carrier = v["carrier"]
            self.state = "DONE"
            self.outcome = "transferred"
            rt = bl.resolve_transfer(self.load)
            self._finish("transferred", rep=rt.get("transfer_to"))
            return self._say(
                "Thanks. I want to get you to a rep directly to finish verification — "
                "one moment while I connect you.",
                instruction="Tell the carrier you're connecting them to a human rep to "
                            "finish verification. Keep it smooth and non-accusatory.")

        self.carrier = v["carrier"]
        self.neg = bl.Negotiation(self.load, max_rounds=self.max_rounds)
        self.state = "STATE_PRICE"
        opening = int(self.neg.open)
        db.log_offer(self.call_id, 0, "agent", opening)
        return self._say(
            f"You're verified, {self.carrier['legal_name']}. "
            f"I've got this one at ${opening}. Does that work for you?",
            instruction=f"Confirm the carrier is verified, then OFFER them ${opening} "
                        "for the load and ask if it works. This is an opening offer.",
            context=f"Carrier: {self.carrier['legal_name']}. Opening offer ${opening}.")

    # -- Steps 4/5: negotiate ---------------------------------------------- #
    def _negotiate(self, text: str) -> str:
        # accept detection (carrier agrees to the rate on the table)
        low = text.lower()
        accept_words = ["that works", "that'll work", "works for me", "deal",
                        "i'll take it", "sounds good", "book it", "agreed",
                        "accept", "perfect", "yes", "yeah", "yep", "yup",
                        "ok", "okay", "sure", "fine"]
        if any(w in low for w in accept_words) and bl.extract_money(text) is None:
            return self._book(self.neg.last_agent_offer)

        # transfer request
        if any(w in low for w in ["talk to a human", "speak to someone", "rep",
                                  "representative", "person", "agent please"]):
            return self._transfer()

        offer = bl.extract_money(text)
        if offer is None:
            return self._say(
                f"What rate are you looking for on load {self.load['load_id']}?",
                instruction="Ask the carrier what rate they are looking for.")

        self.state = "NEGOTIATE"
        db.log_offer(self.call_id, self.neg.round + 1, "carrier", offer)
        result = self.neg.evaluate(offer)

        if result["decision"] == "accept":
            return self._book(result["rate"])

        if result["decision"] == "review":
            # suspiciously cheap -> fraud review, never silently booked (PRD §8.2)
            db.log_note(self.call_id,
                        f"Suspiciously low ask ${int(offer)} on {self.load['load_id']} "
                        f"(fraud tripwire) — routed to review.")
            return self._transfer(reason="fraud_review")

        if result["decision"] == "hold":
            # HIGH first ask -> don't hang up; push back and restate the opening
            rate = int(result["rate"])
            db.log_offer(self.call_id, self.neg.round, "agent", rate)
            return self._say(
                f"Sorry, I can't do ${int(offer)} on this one. I've got it at ${rate} "
                "— can you work with that?",
                instruction=f"Politely say you can't pay ${int(offer)}, and restate your "
                            f"offer of ${rate}. Do NOT reveal any max. Sound like a human "
                            "rep holding firm — friendly but not budging yet.",
                context=f"Hold firm at ${rate}. Never reveal your ceiling/max.")

        if result["decision"] == "no_deal":
            # walked up as far as we can and still apart -> disconnect + clear note
            return self._no_deal(result)

        # counter: walk our offer UP toward (but never past) our cap
        rate = result["rate"]
        db.log_offer(self.call_id, self.neg.round, "agent", rate)
        return self._say(
            f"I hear you. I can come up to ${int(rate)} on this one. "
            "Can you make that work?",
            instruction=f"Acknowledge their ask, then raise your offer to ${int(rate)} "
                        "and ask if that works. Never reveal your maximum. Sound like a "
                        "real freight broker — confident, brief.",
            context=f"Never state your ceiling or max. Your raised offer is ${int(rate)}.")

    # -- Step 6a: book ------------------------------------------------------ #
    def _book(self, rate: float) -> str:
        # FINAL server-side guard (defense in depth — PRD §9.4):
        # never commit to paying the carrier MORE than the ceiling.
        if rate > self.neg.ceiling:
            return self._transfer(reason="ceiling_guard")
        db.book_load(self.load["load_id"])
        db.log_offer(self.call_id, self.neg.round, "agent", rate)
        self.state = "DONE"
        self.outcome = "booked"
        self._finish("booked", rate=rate)
        return self._say(
            f"Done — you're booked on load {self.load['load_id']} at ${int(rate)}. "
            "Rate confirmation is on its way to you. Thanks, and drive safe.",
            instruction=f"Confirm the booking on load {self.load['load_id']} at "
                        f"${int(rate)}, mention a rate confirmation is coming, and close "
                        "warmly.")

    # -- Step 6b: transfer -------------------------------------------------- #
    def _transfer(self, reason: str = "carrier_request") -> str:
        rt = bl.resolve_transfer(self.load)
        rep = rt["transfer_to"]
        self.state = "DONE"
        self.outcome = "transferred"
        self._finish("transferred", rep=rep)
        if rep is None:
            return self._say(
                "Everyone's on the phone right now. I've logged a callback task and a "
                "rep will call you right back. Thanks for your patience.",
                instruction="Explain no rep is available, that you've logged a callback, "
                            "and someone will call them back shortly.")
        # whisper/context summary would be passed to the rep here (PRD §6b)
        return self._say(
            f"Let me connect you with {rep['name']}, who handles this load. "
            "One moment.",
            instruction=f"Tell the carrier you're connecting them to {rep['name']} who "
                        "handles this load.")

    # -- No deal: negotiated, still apart -> clear note + decline + end call -- #
    def _no_deal(self, result: dict) -> str:
        ask = int(result["ask"])
        final = int(result["final_offer"])
        # A clear, human-readable note capturing the whole negotiation (PRD §9.4).
        offers = [f"${int(a)}" for who, a in self.neg.history if who == "agent"]
        followup = ("Carrier's number is within our ceiling — a rep could still "
                    "close it using the reserved buffer."
                    if result.get("within_ceiling")
                    else "Carrier's number is above our ceiling — not workable.")
        db.log_note(
            self.call_id,
            f"NO DEAL on {self.load['load_id']} ({self.load['origin']} -> "
            f"{self.load['destination']}). Carrier {self.carrier['legal_name']} "
            f"(USDOT {self.carrier['usdot_number']}) held at ${ask}. We opened at "
            f"${int(self.neg.open)} and walked up to ${final} "
            f"(offers: {', '.join(offers) or 'n/a'}) over {self.neg.round} rounds; "
            f"no agreement. {followup} Call ended by agent.")
        self.state = "DONE"
        self.outcome = "no_deal"
        self._finish("no_deal")
        return self._say(
            f"I've come up as far as I can on this one and we're still apart, so I "
            "won't be able to make it work today. I'll note it down — thanks for "
            "calling, and let's catch the next one. Take care.",
            instruction="Politely say you've come up as far as you can and you're still "
                        "apart, so you can't make a deal today. Do NOT reveal any "
                        "numbers. Close warmly. Keep it to 1-2 sentences.")

    def _resolve_followup(self, text: str) -> str:
        return "Thanks for calling. Goodbye."

    # -- persistence -------------------------------------------------------- #
    def _finish(self, outcome: str, rep=None, rate=None):
        self.outcome = outcome
        db.end_call(
            self.call_id,
            self.load["load_id"] if self.load else None,
            self.carrier["usdot_number"] if self.carrier else None,
            outcome,
            self.transcript,
        )
        if rep and outcome == "transferred":
            db.log_transfer(self.call_id, rep["rep_id"], "connected")

    def summary(self) -> dict:
        return {
            "call_id": self.call_id,
            "outcome": self.outcome,
            "load_id": self.load["load_id"] if self.load else None,
            "carrier": self.carrier["legal_name"] if self.carrier else None,
            "turns": len(self.transcript),
        }
