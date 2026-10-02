"""A small HTTP server for the task engine, so an app on a phone can list and answer tasks.

It listens on a loopback address only. Reach it from a phone through something that adds TLS and
its own access control in front (``tailscale serve``, an SSH tunnel). Binding it to every
interface would let any device on the network skip that layer, so ``make_server`` refuses it.

Every request needs ``Authorization: Bearer <token>``. The token comes from
``$CLAUDE_HUMAN_TASKS_TOKEN``, or from the token file (``$CLAUDE_HUMAN_TASKS_TOKEN_FILE`` or
``~/.config/claude-human/tasks-token``, made with mode 0600 on first use).

Routes, with the JSON each answers::

    GET  /health                          {"ok": true}
    GET  /pending?supports=a,b&platform=p {"pending": [task, ...]}
    GET  /task/<id>?supports=..&platform=  {"task": task} or 404
    GET  /history?limit=50                {"history": [event, ...]}
    GET  /comments                        {"comments": [...]}    (when the store keeps them)
    POST /done/<id>                       {"done", "still_pending", "detail"}
    POST /open/<id>?supports=..           {"kind", "target", "prepared", "detail"}
    POST /comment/<id>   {"text": ...}    {"ok", "comment", "subject", "intent"} or {"ok": false, "detail"}
    POST /withdraw/<id>  {"reason": ...}  {"ok", "status", "outcome", "reason"}

A task is ``{intent, subject, verb, tier, reason, steps, waiting_s, requires, handoffs, head, ...}``.
``supports`` is what the touchpoint can show (default ``steps``) and ``platform`` where it runs. The
server orders the chain, and the app renders ``head`` without ranking the chain again.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from .engine import TaskEngine

ENV_TOKEN = "CLAUDE_HUMAN_TASKS_TOKEN"
ENV_TOKEN_FILE = "CLAUDE_HUMAN_TASKS_TOKEN_FILE"
DEFAULT_PORT = 8790


def default_token_file() -> Path:
    return Path.home() / ".config" / "claude-human" / "tasks-token"


def load_or_create_token(path: Optional[os.PathLike] = None) -> tuple[str, Path]:
    from ..station.auth import load_or_create_token as _load  # noqa: PLC0415
    return _load(path or os.environ.get(ENV_TOKEN_FILE) or default_token_file(), prefix="tasks_")


def touchpoint(query: str) -> tuple[list[str], str]:
    """What the caller says it can show, and where it runs. Unknown callers get the floor."""
    q = parse_qs(query)
    supports = [x for x in (q.get("supports", [""])[0]).split(",") if x] or ["steps"]
    return supports, (q.get("platform", ["any"])[0] or "any")


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-human-tasks/1"

    @property
    def tasks(self) -> "TaskServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _authed(self) -> bool:
        want = self.tasks.token()
        if not want:
            self._send(503, {"error": "no API token configured"})
            return False
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer ") or not hmac.compare_digest(got[7:].encode(), want.encode()):
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _id(self, path: str, prefix: str) -> str:
        return unquote(path[len(prefix):]).strip("/")

    def do_GET(self):
        if not self._authed():
            return
        u = urlparse(self.path)
        eng = self.tasks.engine
        try:
            if u.path == "/health":
                self._send(200, {"ok": True})
            elif u.path == "/pending":
                sup, plat = touchpoint(u.query)
                self._send(200, {"pending": eng.pending_cards(sup, plat)})
            elif u.path.startswith("/task/"):
                sup, plat = touchpoint(u.query)
                t = eng.store.get(self._id(u.path, "/task/"))
                if t is None:
                    self._send(404, {"error": "not found"})
                else:
                    self._send(200, {"task": eng.card(t, sup, plat)})
            elif u.path == "/history":
                try:
                    limit = int(parse_qs(u.query).get("limit", ["50"])[0])
                except ValueError:
                    limit = 50
                self._send(200, {"history": eng.store.history(max(1, min(limit, 1000)))})
            elif u.path == "/comments" and callable(getattr(eng.store, "comments", None)):
                self._send(200, {"comments": eng.store.comments()})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:                                          # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._authed():
            return
        u = urlparse(self.path)
        eng = self.tasks.engine
        actor = self.tasks.actor
        try:
            if u.path.startswith("/done/"):
                self._send(200, eng.done(self._id(u.path, "/done/")))
            elif u.path.startswith("/open/"):
                sup, plat = touchpoint(u.query)
                self._send(200, eng.open(self._id(u.path, "/open/"), sup, plat))
            elif u.path.startswith("/comment/"):
                body = self._body()
                self._send(200, eng.store.comment(self._id(u.path, "/comment/"),
                                                  str(body.get("text") or ""), actor))
            elif u.path.startswith("/withdraw/"):
                body = self._body()
                reason = str(body.get("reason") or "").strip() or None
                self._send(200, eng.store.withdraw(self._id(u.path, "/withdraw/"), actor, reason))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:                                          # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


class TaskServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine: TaskEngine, token_fn, actor: Optional[str]):
        super().__init__(address, Handler)
        self.engine = engine
        self.token = token_fn
        self.actor = actor


def make_server(engine: TaskEngine, *, token: Optional[str] = None,
                token_file: Optional[os.PathLike] = None, host: str = "127.0.0.1",
                port: int = DEFAULT_PORT, actor: Optional[str] = "owner") -> TaskServer:
    """A server for ``engine`` on a loopback address. ``port=0`` picks a free port.

    The token is ``token``, else read from ``token_file`` at each request (so writing a new one
    takes effect without a restart). ``actor`` is who the token holder is, recorded on comments and
    withdrawals."""
    if not is_loopback(host):
        raise ValueError(f"{host} is not a loopback address. Put TLS and access control in front of "
                         f"127.0.0.1 instead of listening on the network")
    if not token and not token_file:
        raise ValueError("make_server needs a token or a token file")
    path = Path(token_file).expanduser() if token_file else None

    def token_fn() -> str:
        if token:
            return token
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    return TaskServer((host, port), engine, token_fn, actor)
