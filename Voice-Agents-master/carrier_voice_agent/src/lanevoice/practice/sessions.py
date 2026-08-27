"""
Practice sessions — a rep pitching a simulated customer, driven over HTTP.

The deliberate mirror of `dashboard/sessions.py`: same cap, same idle expiry,
same per-session lock, so anyone who has read one manager has read both. What
differs is who's who — here the HUMAN is the rep being evaluated and the MODEL
is the customer — and that practice has no offline mode: a customer with no
model can't hold a conversation worth practicing against, so starting one
without a key fails loudly with the setting named (see `build_persona_chat`).

Voice sessions add two speech legs around the same turn (`practice/speech.py`):
the rep's clip is transcribed before the persona sees it, and the persona's
line is synthesized after. The conversation logic is identical either way —
`_advance` neither knows nor cares how the text arrived — which is what keeps
a voice transcript scoreable by the same phase-2 judge as a text one.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from lanevoice.db.database import Database
from lanevoice.logging_config import get_logger
from lanevoice.practice.acoustics import analyse_clips
from lanevoice.practice.delivery import DeliveryJudge
from lanevoice.practice.judge import JudgeChat, build_judge_chat, score_session
from lanevoice.practice.mailer import ReportMailer, build_report_email
from lanevoice.practice.managers import load_managers
from lanevoice.practice.metrics import compute_metrics
from lanevoice.practice.persona import ChatFn, CustomerPersona, build_persona_chat
from lanevoice.practice.profiles import CustomerProfile, load_profiles, profile_cards
from lanevoice.practice.recording import RECORDING_NAME, stitch_session
from lanevoice.practice.speech import PracticeSpeech
from lanevoice.practice.store import PracticeStore
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

_MAX_SESSIONS = 32          # hard cap on live personas held in memory
_IDLE_EXPIRY_SECS = 2 * 3600
# A rep turn longer than this is nobody holding a push-to-talk button — it is a
# stuck client miscounting. Clamped, not rejected: the audio already went
# through STT, so the turn is real even if the claimed seconds aren't.
_MAX_TURN_SECS = 300.0

# Why a session ended. Stored on the row and shown to the rep.
REASON_ENDED = "ended"            # the rep ended the call from the UI
REASON_HANGUP = "hangup"          # the customer hung up
REASON_TURN_LIMIT = "turn_limit"  # PRACTICE_MAX_TURNS reached
REASON_ABANDONED = "abandoned"    # tab closed / DELETE — nobody said goodbye

# Clip filename extension per upload mime. Only .wav is judgeable (acoustics
# and the delivery model both read WAV); anything else is stored for the
# session's lifetime but skipped by both, silently and on purpose.
_CLIP_EXT = {"audio/wav": ".wav", "audio/ogg": ".ogg",
             "audio/mp4": ".m4a", "audio/mpeg": ".mp3"}


class _Session:
    def __init__(self, session_id: str, persona: CustomerPersona,
                 profile: CustomerProfile, rep_name: str, mode: str,
                 manager_name: str | None = None,
                 manager_email: str | None = None):
        self.id = session_id
        self.persona = persona
        self.profile_full = profile          # the judge needs the answer key
        self.profile = profile.card()
        self.rep_name = rep_name
        self.manager_name = manager_name
        self.manager_email = manager_email
        self.mode = mode                        # text | voice
        self.transcript: list[list[str]] = []   # [["rep"|"customer", line], ...]
        self.rep_turns = 0
        # Talk-time totals for the phase-2 metrics: rep seconds are measured by
        # the browser (it held the button), customer seconds are exact (we made
        # the audio). Zero for text sessions, where word counts stand in.
        self.rep_audio_secs = 0.0
        self.customer_audio_secs = 0.0
        # Clips on disk, in speaking order. `rep_clips` is what the acoustics
        # and the vocal-delivery judge hear (the rep is who's being coached);
        # `all_clips` — both sides interleaved — is what the call recording is
        # stitched from.
        self.clip_dir: Path | None = None
        self.rep_clips: list[Path] = []
        self.all_clips: list[Path] = []
        self.clip_seq = 0
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.last_used = time.monotonic()


class PracticeSessionManager:
    def __init__(self, db: Database, settings: Settings | None = None,
                 chat_factory: Callable[[Settings], ChatFn] | None = None,
                 speech_factory: Callable[[Settings], PracticeSpeech] | None = None,
                 judge_factory: Callable[[Settings], JudgeChat] | None = None,
                 delivery_factory: Callable[[Settings], DeliveryJudge] | None = None,
                 mailer_factory: Callable[[Settings], ReportMailer] | None = None,
                 managers_path: object = None):
        self._settings = settings or get_settings()
        self._store = PracticeStore(db)
        # Clips live NEXT TO THE DATABASE the manager was handed — never a
        # settings-derived path, so a test manager on a temp DB can't spill
        # recordings into a real deployment's directory.
        self._audio_dir = Path(db.path).parent / "practice_audio"
        # Loaded once, at dashboard startup on the real path — a malformed
        # profile or manager file stops the boot with its name, never a
        # mid-call surprise.
        self._profiles = load_profiles()
        self._managers = load_managers(managers_path)
        self._chat_factory = chat_factory or build_persona_chat
        self._mailer_factory = mailer_factory or ReportMailer
        self._speech_factory = speech_factory or PracticeSpeech
        self._judge_factory = judge_factory or build_judge_chat
        self._delivery_factory = delivery_factory or DeliveryJudge
        self._speech_client: PracticeSpeech | None = None
        self._delivery_client: DeliveryJudge | None = None
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def profiles(self) -> list[dict]:
        """Picker cards only — the answer key stays server-side (see `card()`)."""
        return profile_cards(self._profiles)

    def managers(self) -> list[dict]:
        return [m.card() for m in
                sorted(self._managers.values(), key=lambda m: m.name)]

    def reports(self, rep: str | None = None, limit: int = 50,
                offset: int = 0) -> list[dict]:
        return self._store.reports(rep=rep, limit=limit, offset=offset)

    def report_detail(self, session_id: str) -> dict | None:
        detail = self._store.report_detail(session_id)
        if detail is not None:
            detail["has_recording"] = self.recording_path(session_id) is not None
        return detail

    # -- lifecycle ------------------------------------------------------------ #
    def start(self, profile_id: str, rep_name: str, voice: bool = False,
              manager_id: str | None = None) -> dict:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"no such practice profile: {profile_id!r}")
        manager = None
        if manager_id:
            manager = self._managers.get(manager_id)
            if manager is None:
                raise ValueError(f"no such account manager: {manager_id!r} — "
                                 "check practice/data/managers.toml")
        # Both factories may raise RuntimeError naming the missing setting — the
        # server turns that into a 400, and it has to happen BEFORE the session
        # row exists so a misconfigured desk never accumulates ghost sessions.
        chat = self._chat_factory(self._settings)
        speech = self._speech() if voice else None
        persona = CustomerPersona(profile, chat, self._settings)

        session = _Session(
            session_id=uuid.uuid4().hex[:12],
            persona=persona,
            profile=profile,
            rep_name=rep_name,
            mode="voice" if voice else "text",
            manager_name=manager.name if manager else None,
            manager_email=manager.email if manager else None,
        )
        opening = persona.opening_line()
        session.transcript.append(["customer", opening])

        result = {
            "session_id": session.id,
            "rep_name": rep_name,
            "profile": session.profile,
            "opening": opening,
            "mode": session.mode,
            "manager": manager.card() if manager else None,
            "max_turns": self._settings.practice_max_turns,
        }
        if speech is not None:
            # The opening is spoken too — a voice session should never open with
            # a silent bubble. A synthesis failure here fails the start, which is
            # the honest outcome: the voice was asked for and cannot be had.
            wav, mime, secs = speech.synthesize(opening)
            session.customer_audio_secs += secs
            result["audio"] = base64.b64encode(wav).decode("ascii")
            result["audio_mime"] = mime
            self._save_clip(session, wav, mime, "customer")

        with self._lock:
            self._expire_locked()
            if len(self._sessions) >= _MAX_SESSIONS:
                raise RuntimeError(
                    f"Too many open practice sessions ({_MAX_SESSIONS}). "
                    "End some calls first.")
            self._sessions[session.id] = session
        self._store.start(session.id, profile.id, profile.name, rep_name,
                          session.transcript, session.mode,
                          session.manager_name, session.manager_email)
        return result

    def turn(self, session_id: str, text: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            result = self._advance(session, text)
            self._persist(session, result)
            if result["done"]:
                self._attach_report(session, result)
        if result["done"]:
            self._forget(session_id)
        return result

    def turn_voice(self, session_id: str, audio: bytes, mime: str,
                   rep_secs: float) -> dict:
        session = self._get(session_id)
        with session.lock:
            # ValueError on a silent clip propagates as a 400 — the rep re-records
            # and the session is untouched, exactly like an empty text turn.
            heard = self._speech().transcribe(audio, mime)
            result = self._advance(session, heard)
            result["heard"] = heard
            session.rep_audio_secs += max(0.0, min(float(rep_secs or 0.0),
                                                   _MAX_TURN_SECS))
            self._save_clip(session, audio, mime, "rep")
            try:
                wav, out_mime, secs = self._speech().synthesize(result["reply"])
            except RuntimeError as exc:
                # The persona already spoke (and was billed for) this turn; a
                # broken voice must not eat the words. Text-only beats a 500.
                logger.warning("Practice TTS failed mid-session; replying "
                               "text-only: %s", exc)
                result["audio_error"] = str(exc)
            else:
                session.customer_audio_secs += secs
                result["audio"] = base64.b64encode(wav).decode("ascii")
                result["audio_mime"] = out_mime
                self._save_clip(session, wav, out_mime, "customer")
            self._persist(session, result)
            if result["done"]:
                self._attach_report(session, result)
        if result["done"]:
            self._forget(session_id)
        return result

    def end(self, session_id: str) -> dict:
        """The rep hung up: finalize the record, score it, hand back the summary."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(session_id)
        with session.lock:
            self._finish(session, REASON_ENDED)
            summary = self._summary(session, REASON_ENDED)
            report = self._score(session, REASON_ENDED)
            if report is not None:
                summary["report"] = report
            return summary

    def abandon(self, session_id: str) -> bool:
        """Tab closed / DELETE. Finalizes like `end` but is a quiet no-op on an
        unknown id — an abandon retried after expiry shouldn't be an error."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        with session.lock:
            self._finish(session, REASON_ABANDONED)
            self._cleanup_clips(session)
        return True

    # -- internals -------------------------------------------------------------#
    def _advance(self, session: _Session, text: str) -> dict:
        """One rep turn through the persona. Caller holds the session lock and
        persists afterwards — this only moves the conversation."""
        session.last_used = time.monotonic()
        line, hung_up = session.persona.reply(session.transcript, text)
        session.transcript.append(["rep", text])
        session.transcript.append(["customer", line])
        session.rep_turns += 1
        hit_cap = session.rep_turns >= self._settings.practice_max_turns
        done = hung_up or hit_cap
        result = {"reply": line, "done": done, "turns": session.rep_turns}
        if done:
            reason = REASON_HANGUP if hung_up else REASON_TURN_LIMIT
            result["end_reason"] = reason
            result["summary"] = self._summary(session, reason)
        return result

    def _persist(self, session: _Session, result: dict) -> None:
        if result["done"]:
            self._finish(session, result["end_reason"])
        else:
            self._store.record(session.id, session.transcript, session.rep_turns,
                               session.rep_audio_secs, session.customer_audio_secs)

    def _finish(self, session: _Session, reason: str) -> None:
        self._store.finish(session.id, reason, session.transcript,
                           session.rep_turns, session.rep_audio_secs,
                           session.customer_audio_secs)

    def _attach_report(self, session: _Session, result: dict) -> None:
        report = self._score(session, result["end_reason"])
        if report is not None:
            result["summary"]["report"] = report

    def _score(self, session: _Session, reason: str) -> dict | None:
        """Judge + metrics for a finished session, stored either way.

        Not for abandoned sessions (nobody is reading the answer) and not for
        zero-turn ones (there is nothing to grade). A judge failure lands as a
        `judge_error` row — the transcript is already on disk, so a re-score is
        always possible, and the rep's end-of-call response must never be a 500
        because the grader was down. Voice sessions additionally get the
        acoustics and the vocal-delivery verdict, and their clips are deleted
        afterwards unless PRACTICE_KEEP_AUDIO says otherwise.
        """
        try:
            if session.rep_turns < 1 or reason == REASON_ABANDONED:
                return None
            report: dict
            metrics = compute_metrics(
                session.transcript, session.mode, session.rep_audio_secs,
                session.customer_audio_secs, time.monotonic() - session.started,
                reason)
            acoustic = analyse_clips(session.rep_clips) if session.rep_clips else None
            if acoustic:
                metrics.update(acoustic)
            try:
                chat = self._judge_factory(self._settings)
                report = score_session(session.profile_full, session.transcript,
                                       chat, self._settings)
            except Exception as exc:  # noqa: BLE001 - any judge failure degrades the same way
                logger.warning("Practice judge failed for session %s: %s",
                               session.id, exc)
                report = {"judge_error": str(exc)}
            if (session.mode == "voice" and session.rep_clips
                    and self._settings.practice_delivery_model):
                report["delivery"] = self._score_delivery(session)
            report["metrics"] = metrics
            report["judge_model"] = self._settings.resolved_llm_model
            report["session_id"] = session.id
            self._store.save_report(session.id, report)
            # The replayable conversation, both sides in order — built after the
            # judges have heard the raw clips, before cleanup takes them away.
            if session.mode == "voice" and session.all_clips and session.clip_dir:
                stitch_session(session.clip_dir, session.all_clips)
            self._email_report(session, report)
            return report
        finally:
            self._cleanup_clips(session)

    def _email_report(self, session: _Session, report: dict) -> None:
        """The scorecard to the manager the rep picked — recorded either way,
        and NON-FATAL either way: a dead mail server costs the manager an
        email, never the rep their scorecard."""
        if not session.manager_email:
            return
        if not self._settings.uses_practice_email:
            error = ("email not configured: set SMTP_HOST and SMTP_FROM to mail "
                     "reports to managers")
            self._store.record_email(session.id, error=error)
            report["email_status"] = {"error": error}
            return
        try:
            msg = build_report_email(
                rep_name=session.rep_name, profile_name=session.profile["name"],
                manager_name=session.manager_name or "",
                manager_email=session.manager_email, report=report,
                mode=session.mode, sender=self._settings.smtp_from)
            self._mailer_factory(self._settings).send(msg)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised upward
            logger.warning("Report email failed for session %s: %s", session.id, exc)
            self._store.record_email(session.id, error=str(exc))
            report["email_status"] = {"error": str(exc)}
        else:
            self._store.record_email(session.id, emailed_to=session.manager_email)
            report["email_status"] = {"emailed_to": session.manager_email,
                                      "manager_name": session.manager_name}

    def _score_delivery(self, session: _Session) -> dict:
        try:
            if self._delivery_client is None:
                self._delivery_client = self._delivery_factory(self._settings)
            return self._delivery_client.score(session.rep_clips)
        except Exception as exc:  # noqa: BLE001 - same degradation as the text judge
            logger.warning("Delivery judge failed for session %s: %s",
                           session.id, exc)
            return {"delivery_error": str(exc)}

    def recording_path(self, session_id: str) -> Path | None:
        """The stitched call recording, if this session has one. File-backed on
        purpose: a recording outlives the in-memory session and the process."""
        path = self._audio_dir / session_id / RECORDING_NAME
        return path if path.is_file() else None

    def _save_clip(self, session: _Session, audio: bytes, mime: str,
                   who: str) -> None:
        """One clip onto disk, best-effort: a full disk must cost the vocal
        verdict and the replay, never the turn that already happened."""
        try:
            if session.clip_dir is None:
                session.clip_dir = self._audio_dir / session.id
                session.clip_dir.mkdir(parents=True, exist_ok=True)
            ext = _CLIP_EXT.get(mime.split(";")[0].strip(), ".webm")
            path = session.clip_dir / f"{session.clip_seq:03d}_{who}{ext}"
            session.clip_seq += 1
            path.write_bytes(audio)
            session.all_clips.append(path)
            if who == "rep":
                session.rep_clips.append(path)
        except OSError as exc:
            logger.warning("Could not store practice clip for session %s: %s",
                           session.id, exc)

    def _cleanup_clips(self, session: _Session) -> None:
        """Raw per-turn clips are kept only as long as scoring needs them,
        unless the desk opted into keeping them. The stitched `call.wav`
        SURVIVES cleanup — it is the replayable conversation the play button
        serves — so only sessions that never produced one lose their folder."""
        if self._settings.practice_keep_audio or session.clip_dir is None:
            return
        for path in session.clip_dir.glob("*"):
            if path.name != RECORDING_NAME:
                path.unlink(missing_ok=True)
        try:
            session.clip_dir.rmdir()       # only succeeds with no recording kept
        except OSError:
            pass
        session.clip_dir = None
        session.rep_clips = []
        session.all_clips = []

    def _forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _speech(self) -> PracticeSpeech:
        """One speech client for the manager's lifetime, built on first voice
        use — construction warms up the TTS voice (a real request), so text-only
        desks never pay for it and a broken voice config surfaces on the first
        voice session, not at boot."""
        if self._speech_client is None:
            self._speech_client = self._speech_factory(self._settings)
        return self._speech_client

    def _summary(self, session: _Session, reason: str) -> dict:
        return {
            "session_id": session.id,
            "rep_name": session.rep_name,
            "profile_id": session.profile["id"],
            "profile_name": session.profile["name"],
            "mode": session.mode,
            "turns": session.rep_turns,
            "end_reason": reason,
            "duration_secs": round(time.monotonic() - session.started),
        }

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
