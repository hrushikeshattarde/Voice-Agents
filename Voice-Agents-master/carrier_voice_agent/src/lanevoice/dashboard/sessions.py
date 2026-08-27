"""
Browser playground sessions — the real agent, driven over HTTP.

Each session holds a live `CarrierSalesAgent` built exactly the way the demo
builds one: the same repository resolution (`build_repository`), the same
composer fallback (real model when configured, `StubComposer` otherwise), the
same settings. Nothing here is a mock — a booking agreed in the playground goes
through the identical code path a phone booking would, which is the point.

Offline is the default and pins `DATA_SOURCE=sqlite` the way `lanevoice-demo`
does; `live=True` honours the configured data source instead, so a playground
call can be driven against the real Transport Pro board (and, like the demo,
WILL post a real offer if a booking is taken all the way through).
"""

from __future__ import annotations

import threading
import time
import uuid

from lanevoice.conversation import CarrierSalesAgent
from lanevoice.datasource import build_repository
from lanevoice.settings import Settings, get_settings

_MAX_SESSIONS = 32          # hard cap on live agents held in memory
_IDLE_EXPIRY_SECS = 2 * 3600

# First line of the note every playground call gets. The runs list keys its
# "playground vs phone" source column off this prefix, so a dashboard test call
# can never be mistaken for a carrier who actually rang the desk.
PLAYGROUND_NOTE_PREFIX = "Playground test call"


class FactsRecorder:
    """Composer wrapper that captures each turn's directive/facts/speakable.

    The same idea as the demo's `--facts` flag: the FACTS block is the fetched
    data verbatim — the only load, carrier and rate values the agent was allowed
    to speak — which answers "did it pull the right information" more directly
    than the sentence the model made of it.
    """

    def __init__(self, inner):
        self._inner = inner
        self.turns: list[dict] = []

    def compose(self, directive: str, facts: str = "", dialogue: str = "",
                speakable: str = "", correction: str = "") -> str:
        self.turns.append({"directive": " ".join(directive.split()),
                           "facts": facts, "speakable": speakable})
        return self._inner.compose(directive=directive, facts=facts,
                                   dialogue=dialogue, speakable=speakable,
                                   correction=correction)

    def read(self, dialogue: str, fields: dict[str, str]) -> dict:
        return self._inner.read(dialogue, fields)


def _quiet_composer(settings: Settings) -> tuple[object, str]:
    """The demo's composer fallback, minus the prints: (composer, label)."""
    from lanevoice.voice import StubComposer, build_composer

    reason = None
    if not settings.use_llm:
        reason = "USE_LLM=false"
    elif not settings.llm_api_key:
        reason = f"no {settings.llm_key_name} set"
    if reason:
        return StubComposer(settings), f"offline stub — {reason}"
    try:
        composer = build_composer(settings)
    except Exception as exc:  # noqa: BLE001 - degrade exactly like the demo
        return (StubComposer(settings),
                f"offline stub — {settings.llm_provider} failed to start ({exc})")
    return composer, f"{settings.llm_provider} / {settings.resolved_llm_model}"


class _Session:
    def __init__(self, session_id: str, agent: CarrierSalesAgent,
                 recorder: FactsRecorder, meta: dict):
        self.id = session_id
        self.agent = agent
        self.recorder = recorder
        self.meta = meta
        self.lock = threading.Lock()
        self.last_used = time.monotonic()


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------ #
    def start(self, live: bool = False) -> dict:
        settings = get_settings()
        if not live:
            # Same pinning as the demo: scripted against the seed board unless
            # the caller explicitly asked for the configured data source.
            settings = settings.model_copy(update={"data_source": "sqlite"})
        composer, composer_label = _quiet_composer(settings)
        recorder = FactsRecorder(composer)
        # May raise RuntimeError on missing live credentials — the server turns
        # that into a 400 with the message, which names the setting to fill.
        repo = build_repository(settings)
        agent = CarrierSalesAgent(repo, recorder, settings)
        repo.log_note(
            agent.call_id,
            f"{PLAYGROUND_NOTE_PREFIX} — started from the dashboard UI "
            f"({'live board' if live else 'seed board'}), not the phone line.")

        session = _Session(
            session_id=uuid.uuid4().hex[:12],
            agent=agent,
            recorder=recorder,
            meta={
                "live": live,
                "data_source": settings.data_source if live else "sqlite",
                "composer": composer_label,
            },
        )
        with self._lock:
            self._expire_locked()
            if len(self._sessions) >= _MAX_SESSIONS:
                raise RuntimeError(
                    f"Too many open playground sessions ({_MAX_SESSIONS}). "
                    "End some calls first.")
            self._sessions[session.id] = session

        greeting = agent.greeting()
        return {
            "session_id": session.id,
            "call_id": agent.call_id,
            "greeting": greeting,
            "state": agent.state.value,
            **session.meta,
        }

    def turn(self, session_id: str, text: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            session.last_used = time.monotonic()
            facts_from = len(session.recorder.turns)
            reply = session.agent.handle(text)
            done = session.agent.state.value == "done"
            result = {
                "reply": reply,
                "state": session.agent.state.value,
                "done": done,
                # What the composer was handed for THIS reply — data, not prose.
                "facts": [t for t in session.recorder.turns[facts_from:]],
            }
            if done:
                result["summary"] = session.agent.summary()
        if done:
            self.end(session_id)
        return result

    def end(self, session_id: str) -> bool:
        """Hang up: finalize the record exactly the way a phone hangup does.

        `abandon()` persists the transcript and marks the outcome ABANDONED —
        and is a no-op on a call that already concluded properly."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        with session.lock:
            session.agent.abandon()
        return True

    # -- internals -------------------------------------------------------------#
    def _get(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def _expire_locked(self) -> None:
        cutoff = time.monotonic() - _IDLE_EXPIRY_SECS
        for sid in [sid for sid, s in self._sessions.items()
                    if s.last_used < cutoff]:
            del self._sessions[sid]
