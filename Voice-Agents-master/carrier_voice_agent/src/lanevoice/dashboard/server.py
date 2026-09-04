"""
`lanevoice-dashboard` — the web dashboard over the call audit trail.

Stdlib HTTP server, deliberately: the dashboard has the same install story as
`make demo` (no extra dependencies, no keys), and the traffic is one operator's
browser, not the internet. It binds to 127.0.0.1 unless told otherwise.

    lanevoice-dashboard                 # http://127.0.0.1:8710
    lanevoice-dashboard --port 9000
    lanevoice-dashboard --host 0.0.0.0  # only on a network you trust: the
                                        # playground can reach the live board

Routes:
    GET  /api/overview                KPIs, outcome split, calls per day
    GET  /api/calls                   run list (?outcome=&label=&q=&limit=&offset=)
    GET  /api/calls/<id>              transcript, offers, notes, transfers
    GET  /api/calls/<id>/recording    the call audio (RECORD_CALLS=true), OGG
    GET  /api/loads                   the local board (seed data offline;
                                      local copies in live mode)
    GET  /api/config                  models + negotiation + integrations
                                      (booleans for keys — never the secrets)
    POST /api/playground/sessions     start a call ({"live": true} to honour
                                      DATA_SOURCE instead of the seed board)
    POST /api/playground/sessions/<id>/turns    one caller turn ({"text": ...})
    DELETE /api/playground/sessions/<id>        hang up (session forgotten)
    POST /api/board/reset             re-seed the demo board, keep call history
    GET  /api/practice/profiles       customer moods a rep can pitch against
                                      (picker cards only — never the answer key)
    GET  /api/practice/managers       account managers a report can be mailed to
                                      (from practice/data/managers.toml)
    POST /api/practice/sessions       start practicing ({"profile_id", "rep_name",
                                      "voice": true, "manager_id": optional}) —
                                      voice adds opening audio; a manager means
                                      the scored report is emailed to them
    POST /api/practice/sessions/<id>/turns      one REP turn. JSON {"text": ...},
                                      or a raw audio/* body (the recorded clip,
                                      X-Audio-Seconds header) for a voice turn —
                                      transcribed, answered in character, reply
                                      synthesized back as base64 WAV
    POST /api/practice/sessions/<id>/end        rep hangs up → summary + scorecard
    DELETE /api/practice/sessions/<id>          abandon (recorded, not scored)
    GET  /api/practice/reports        finished sessions with their scorecard
                                      headlines (?rep=&limit=&offset=)
    GET  /api/practice/reports/<id>   one session's full story: scorecard,
                                      metrics, transcript
    GET  /api/practice/sessions/<id>/recording   the stitched call audio
                                      (voice sessions), served as WAV
"""

from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lanevoice.dashboard.queries import DashboardQueries
from lanevoice.dashboard.sessions import SessionManager
from lanevoice.datasource import open_database
from lanevoice.logging_config import get_logger
from lanevoice.practice.sessions import PracticeSessionManager
from lanevoice.settings import Settings, get_settings

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
_MAX_BODY = 64 * 1024
# Voice turns only. A push-to-talk clip is webm/opus at roughly 10-20KB/s, so a
# two-minute turn is well under 4MB — this is headroom for format surprises, not
# an invitation. Every JSON route keeps the 64KB cap above.
_MAX_AUDIO_BODY = 4 * 1024 * 1024


class DashboardApp:
    """Everything the request handler needs, built once at startup."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        # Same rule as the worker: the sample board offline, and with Transport
        # Pro as the system of record an audit DB that carries no sample rows and
        # reads its rep directory from REPS_FILE. See `datasource.open_database`.
        db = open_database(self.settings)
        self.queries = DashboardQueries(db)
        self.sessions = SessionManager()
        # Loads and validates the profile TOMLs here, at boot — a malformed
        # profile stops the dashboard with the file named, never a mid-call 500.
        self.practice = PracticeSessionManager(db, self.settings)

    def config_summary(self) -> dict:
        """What the Settings page shows. Key PRESENCE only — never a secret."""
        s = self.settings
        return {
            "data_source": s.data_source,
            "db_path": s.db_path,
            "models": {
                "stt": s.stt_model,
                "llm_provider": s.llm_provider,
                "llm": s.resolved_llm_model,
                "llm_key_present": bool(s.llm_api_key),
                "use_llm": s.use_llm,
                "tts": s.tts_model,
                "tts_voice": s.tts_voice,
            },
            "negotiation": {
                "max_rounds": s.max_negotiation_rounds,
                "buffer": s.negotiation_buffer,
                "reciprocity": s.negotiation_reciprocity,
                "discretion_rate": s.negotiation_discretion_rate,
                "settle_gap_rate": s.negotiation_settle_gap_rate,
                "split_gap_rate": s.negotiation_split_gap_rate,
                "stonewall_final_rate": s.negotiation_stonewall_final_rate,
                "max_holds": s.negotiation_max_holds,
            },
            "transport_pro": {
                "enabled": s.uses_transport_pro,
                "url": s.transport_pro_url or None,
                "office_terminal_code": s.transport_pro_office_terminal_code or None,
                "open_load_statuses": sorted(s.open_load_statuses),
                "max_offered_loads": s.transport_pro_max_offered_loads,
            },
            "integrations": {
                "highway": s.uses_highway,
                "happyrobot_booking_links": s.uses_happyrobot,
                "livekit": bool(s.livekit_url),
            },
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "lanevoice-dashboard"

    @property
    def app(self) -> DashboardApp:
        return self.server.app  # type: ignore[attr-defined]

    # -- plumbing --------------------------------------------------------------#
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _drain(self, length: int) -> None:
        """Consume an oversized request body before erroring. Left unread on the
        socket, Windows resets the connection and the client sees an abort
        instead of the 400 that names the problem. Bounded so a hostile
        Content-Length can't make us read forever."""
        self.rfile.read(min(length, 2 * _MAX_AUDIO_BODY))

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self._drain(length)
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _read_audio_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_AUDIO_BODY:
            self._drain(length)
            raise ValueError("audio clip too large — keep a turn under about two minutes")
        if not length:
            raise ValueError("empty audio body")
        return self.rfile.read(length)

    def _send_static(self, rel: str) -> None:
        target = (_STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(_STATIC_DIR.resolve())) or not target.is_file():
            self._send_error_json(404, "not found")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routing ---------------------------------------------------------------#
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - a broken page must say so, not hang
            logger.exception("GET %s failed", self.path)
            self._send_error_json(500, "internal error — see the server log")

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_error_json(400, str(exc))
        except KeyError:
            self._send_error_json(404, "no such session — it may have expired")
        except RuntimeError as exc:
            # Missing credentials, session cap: the message names the fix.
            self._send_error_json(400, str(exc))
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("POST %s failed", self.path)
            self._send_error_json(500, "internal error — see the server log")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/playground/sessions/([\w-]+)", path)
        if m:
            self._send_json({"ended": self.app.sessions.end(m.group(1))})
            return
        m = re.fullmatch(r"/api/practice/sessions/([\w-]+)", path)
        if m:
            self._send_json({"ended": self.app.practice.abandon(m.group(1))})
            return
        self._send_error_json(404, "not found")

    def _route_get(self) -> None:
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            return self._send_static("index.html")
        if path.startswith("/static/"):
            return self._send_static(path[len("/static/"):])

        if path == "/api/overview":
            days = int(qs.get("days", ["30"])[0])
            return self._send_json(self.app.queries.overview(days=max(7, min(days, 90))))
        if path == "/api/calls":
            return self._send_json(self.app.queries.calls(
                outcome=qs.get("outcome", [None])[0] or None,
                label=qs.get("label", [None])[0] or None,
                q=qs.get("q", [None])[0] or None,
                limit=int(qs.get("limit", ["100"])[0]),
                offset=int(qs.get("offset", ["0"])[0]),
            ))
        m = re.fullmatch(r"/api/calls/([\w-]+)", path)
        if m:
            detail = self.app.queries.call_detail(m.group(1))
            if detail is None:
                return self._send_error_json(404, "no such call")
            return self._send_json(detail)
        m = re.fullmatch(r"/api/calls/([\w-]+)/recording", path)
        if m:
            recording = self.app.queries.recording_path(m.group(1))
            if recording is None:
                return self._send_error_json(404, "no recording for this call")
            body = recording.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/ogg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/loads":
            return self._send_json({
                "data_source": self.app.settings.data_source,
                "live_board": self.app.settings.uses_transport_pro,
                "loads": self.app.queries.loads(),
            })
        if path == "/api/config":
            return self._send_json(self.app.config_summary())
        if path == "/api/practice/profiles":
            return self._send_json({"profiles": self.app.practice.profiles()})
        if path == "/api/practice/managers":
            return self._send_json({
                "managers": self.app.practice.managers(),
                "email_configured": self.app.settings.uses_practice_email,
            })
        if path == "/api/practice/reports":
            return self._send_json({"reports": self.app.practice.reports(
                rep=qs.get("rep", [None])[0] or None,
                limit=int(qs.get("limit", ["50"])[0]),
                offset=int(qs.get("offset", ["0"])[0]),
            )})
        m = re.fullmatch(r"/api/practice/reports/([\w-]+)", path)
        if m:
            detail = self.app.practice.report_detail(m.group(1))
            if detail is None:
                return self._send_error_json(404, "no such practice session")
            return self._send_json(detail)
        m = re.fullmatch(r"/api/practice/sessions/([\w-]+)/recording", path)
        if m:
            recording = self.app.practice.recording_path(m.group(1))
            if recording is None:
                return self._send_error_json(404, "no recording for this session")
            body = recording.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_error_json(404, "not found")

    def _route_post(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/playground/sessions":
            body = self._read_body()
            return self._send_json(self.app.sessions.start(
                live=bool(body.get("live"))), 201)

        m = re.fullmatch(r"/api/playground/sessions/([\w-]+)/turns", path)
        if m:
            body = self._read_body()
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("'text' is required")
            return self._send_json(self.app.sessions.turn(m.group(1), text))

        if path == "/api/board/reset":
            if self.app.settings.uses_transport_pro:
                # The reset re-writes the SAMPLE board. Live, the board is
                # Transport Pro's and the sample rows must never reappear.
                raise ValueError("The board is Transport Pro's in this deployment; "
                                 "there is no sample board to reset.")
            self.app.queries.reset_board()
            return self._send_json({"reset": True})

        if path == "/api/practice/sessions":
            body = self._read_body()
            profile_id = str(body.get("profile_id") or "").strip()
            rep_name = str(body.get("rep_name") or "").strip()
            if not profile_id:
                raise ValueError("'profile_id' is required")
            if not rep_name:
                raise ValueError("'rep_name' is required")
            return self._send_json(self.app.practice.start(
                profile_id, rep_name, voice=bool(body.get("voice")),
                manager_id=str(body.get("manager_id") or "").strip() or None), 201)

        m = re.fullmatch(r"/api/practice/sessions/([\w-]+)/turns", path)
        if m:
            ctype = (self.headers.get("Content-Type") or "").lower()
            if ctype.startswith("audio/"):
                # A voice turn: the raw recorded clip, with the browser's own
                # measure of how long the rep held the button.
                clip = self._read_audio_body()
                try:
                    secs = float(self.headers.get("X-Audio-Seconds") or 0.0)
                except ValueError:
                    secs = 0.0
                return self._send_json(
                    self.app.practice.turn_voice(m.group(1), clip, ctype, secs))
            body = self._read_body()
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("'text' is required")
            return self._send_json(self.app.practice.turn(m.group(1), text))

        m = re.fullmatch(r"/api/practice/sessions/([\w-]+)/end", path)
        if m:
            return self._send_json({"summary": self.app.practice.end(m.group(1))})
        self._send_error_json(404, "not found")


def serve(host: str, port: int, app: DashboardApp) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    from lanevoice.env import load_env
    from lanevoice.logging_config import setup_logging

    parser = argparse.ArgumentParser(description="LaneVoice operations dashboard")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1 — local only)")
    # 8710 is the documented default, but a supervisor that hands us a port
    # does it through $PORT — honour that so the dashboard comes up on the
    # port it was actually given rather than one already in use.
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT") or 8710))
    args = parser.parse_args()

    # Before the first get_settings(): that result is cached.
    load_env(verbose=True)
    setup_logging(get_settings().log_level)

    app = DashboardApp()
    print(f"[data source: {app.settings.data_source} — audit trail in "
          f"{app.settings.db_path}]")
    httpd = serve(args.host, args.port, app)
    print(f"LaneVoice dashboard: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
