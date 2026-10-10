"""The station's HTTP server: a live picture of one window and the input that drives it.

Routes.

    GET  /view      the page (static, no secret, served no-store)
    GET  /health    {"ok", "locked", "trusted", "windows"} (the window count for a master grant only)
    GET  /windows   every on-screen window (master grant only)
    GET  /shot      one JPEG frame
    GET  /stream    multipart/x-mixed-replace, one JPEG per part
    POST /input     one input message, see ``QuartzInput.do_input``

Rules the server keeps whatever the auth says.

- It binds to the loopback interface only. Every click it accepts is a real click on the Mac, so it
  is reached through something that adds its own authentication and encryption in front of it (a
  private network proxy, an SSH tunnel), never directly.
- The token is read from the ``Authorization: Bearer`` header and from nowhere else. A token in a
  query string is ignored, so a request that carries one is unauthorised.
- A pinned grant drives the window of the app its station names, whatever the request asks for. It
  cannot list windows, choose a window id, or send input to another app.
- A stream for an app that cannot be found ends. It never falls back to the whole display, because
  that would show every other window to someone who was given one.
- A refusal says only "unauthorized" to the caller. The reason goes to the server's log.
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .auth import Grant, StationAuth, Unavailable
from .view import render

LOOPBACK = ("127.0.0.1", "localhost")
DEFAULT_PORT = 8789
DEFAULT_FPS = 8.0
BOUNDARY = b"stationframe"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Station:
    """Everything a request needs: the auth, the screen, the input sink and the page."""

    def __init__(self, auth: StationAuth, *, screen: Any = None, input: Any = None,  # noqa: A002
                 fps: float = DEFAULT_FPS, view_html: Optional[str] = None,
                 log=_log, server_name: str = "claude-human-station/1"):
        if screen is None:
            screen = default_screen()
        if input is None:
            from .input import QuartzInput  # noqa: PLC0415
            input = QuartzInput()  # noqa: A001
        self.auth = auth
        self.screen = screen
        self.input = input
        self.fps = fps
        self.view_html = view_html if view_html is not None else render()
        self.log = log
        self.server_name = server_name


class _Access:
    """What the current request may do: the grant, and the app (and window) it is pinned to."""

    def __init__(self, grant: Grant, app: Optional[str], window: Optional[int] = None):
        self.grant = grant
        self.master = grant.master
        self.app = app
        self.window = window


def default_screen() -> Any:
    """``claude_human.screenshot`` with its default capture settings."""
    from .. import screenshot  # noqa: PLC0415
    return screenshot


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-human-station/1"

    @property
    def st(self) -> Station:
        return self.server.station  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ helpers

    def version_string(self):
        return self.st.server_name

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

    def _bearer(self) -> str:
        raw = self.headers.get("Authorization", "")
        return raw[7:] if raw.startswith("Bearer ") else ""

    def _refuse(self, why: str) -> None:
        self.st.log(f"401 {self.command} {self.path.split('?')[0]}: {why}")
        self._send(401, {"error": "unauthorized"})

    def _authed(self) -> Optional[_Access]:
        presented = self._bearer()
        if not presented:
            self._refuse("no bearer token")
            return None
        try:
            grant = self.st.auth.check(presented)
        except Exception as e:  # noqa: BLE001 - an auth that cannot answer grants nothing
            self._refuse(f"the auth could not answer ({type(e).__name__}: {e})")
            return None
        if grant is None:
            self._refuse("bearer present, not accepted")
            return None
        if grant.master:
            return _Access(grant, None)
        try:
            app = self.st.auth.app_for(grant)
            window = self.st.auth.window_for(grant) if app else None
        except Exception as e:  # noqa: BLE001
            # A station whose app cannot be read is not a station with no app. None would mean the
            # whole display, the widest access, reached through the narrowest failure. The same
            # holds for its window: unreadable is not "any window of the app".
            kind = "unavailable" if isinstance(e, Unavailable) else type(e).__name__
            self._refuse(f"could not read the station's app or window ({kind}: {e})")
            return None
        return _Access(grant, app, int(window) if window else None)

    # ------------------------------------------------------------------ GET

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _serve_view(self):
        body = self.st.view_html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # no-store, or a WebView keeps the old page (and its old gesture code) after an update
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/view":
            try:
                self._serve_view()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        who = self._authed()
        if not who:
            return
        q = parse_qs(u.query)
        screen = self.st.screen

        def target():
            # a pinned grant names its app, and maybe its window; only a master grant chooses
            if who.master:
                return q.get("app", [None])[0], (int(q.get("window", ["0"])[0]) or None)
            return who.app, who.window

        try:
            if u.path == "/health":
                self._send(200, {"ok": True, "locked": bool(screen.screen_locked()),
                                 "trusted": bool(self.st.input.can_act()),
                                 "windows": len(screen.windows()) if who.master else None})
            elif u.path == "/windows":
                if not who.master:
                    self._send(403, {"error": "this grant covers one station"})
                    return
                self._send(200, {"windows": screen.windows()})
            elif u.path == "/shot":
                app, wid = target()
                resolved = screen.resolve(app, wid)
                f = None if (app and resolved is None) else screen.frame(resolved)
                if not f:
                    self._send(404, {"error": "nothing to capture"})
                    return
                jpg, w, h = f
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(jpg)))
                self.send_header("X-Frame-Size", f"{w}x{h}")
                self._cors()
                self.end_headers()
                self.wfile.write(jpg)
            elif u.path == "/stream":
                app, wid = target()
                self._stream(app, wid, float(q.get("fps", [self.st.fps])[0]))
            else:
                self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, {"error": str(e)[:200]})
            except Exception:  # noqa: BLE001
                pass

    def _stream(self, app: Optional[str], wid: Optional[int], fps: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY.decode())
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        delay = 1.0 / max(1.0, min(fps, 20.0))
        screen = self.st.screen
        while True:
            # looked up again every frame, so the stream follows the app through a relaunch
            resolved = screen.resolve(app, wid)
            if app and resolved is None:
                break          # the app is not on the visible desktop: end, never widen
            f = screen.frame(resolved)
            if not f:
                break
            jpg = f[0]
            self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                             b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n")
            self.wfile.write(jpg)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            time.sleep(delay)

    # ------------------------------------------------------------------ POST

    def do_POST(self):
        u = urlparse(self.path)
        who = self._authed()
        if not who:
            return
        if u.path != "/input":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(msg, dict):
                raise ValueError("an input is a JSON object")
            if not who.master:
                # the grant decides where the input lands, not the body of the request
                msg["app"] = who.app
                msg.pop("window", None)
                if who.window:
                    msg["window"] = who.window
            sink = self.st.input
            if msg.get("type") == "spotlight":
                result = self.st.auth.perform(who.grant, "spotlight", lambda: sink.do_input(msg))
            else:
                result = sink.do_input(msg)
        except Exception as e:  # noqa: BLE001
            self._send(400, {"ok": False, "detail": str(e)[:200]})
            return
        try:
            self.st.auth.on_input(who.grant, msg, result)
        except Exception as e:  # noqa: BLE001 - a hook that fails does not undo the input
            self.st.log(f"on_input hook failed: {type(e).__name__}: {e}")
        self._send(200, result)


class StationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, station: Station):
        self.station = station
        super().__init__(address, Handler)


def make_server(auth: StationAuth, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                **station_kw: Any) -> StationServer:
    """A server for ``auth``, bound to the loopback interface. Port 0 picks a free port.

    Any other host is refused. Put an authenticating proxy in front of the station to reach it from
    another device."""
    if host not in LOOPBACK:
        raise ValueError(f"the station binds to the loopback interface only, not {host!r}")
    if not isinstance(auth, StationAuth):
        raise TypeError("auth must be a StationAuth")
    return StationServer(("127.0.0.1", port), Station(auth, **station_kw))
