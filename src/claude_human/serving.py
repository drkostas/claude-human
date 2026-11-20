"""serving.py — a local HTTP server that finishes the requests it is answering before it stops, and
a request body read with a limit.

A service manager stops a server with SIGTERM, and Python's default answer to SIGTERM ends the
process at once. A `ThreadingHTTPServer` answers each request on a daemon thread, so every request
in progress dies with it and its caller reads a closed connection, even when the stop was a planned
restart. `DrainingMixin` counts the requests in progress, and `serve_until_stopped` turns SIGTERM
into a drain: the server stops accepting, the open requests get up to `drain_s` seconds to finish,
then the server closes. A service manager waits for the process to exit before it kills it (launchd
waits 20 seconds by default), so a drain shorter than that loses nothing.

A caller still meets the short gap between the old process and the new one. `retry_refused` repeats
a call whose connection was refused, which means the request never reached any server, so it is
safe for any method. A request that reached the server and then lost its connection is never
repeated here, because its effect is unknown.

    class MyServer(DrainingMixin, ThreadingHTTPServer): ...
    serve_until_stopped(MyServer(("127.0.0.1", 8000), Handler), drain_s=10)

    answer = retry_refused(lambda: urllib.request.urlopen(url, timeout=5).read())

A handler that reads a request body takes the size from the caller's Content-Length, so a caller
decides how much the handler reads, and a value that is not a number raises inside the handler
where nothing answers it. `read_body` checks the header first and refuses with a status to send
back (`BodyRefused`: 400 for a header that is not a whole number or a body that ends early, 413
for a body over the cap), and `json_object` turns the bytes into one JSON object or refuses with
a 400.

    def do_POST(self):
        try:
            body = serving.json_object(serving.read_body(self, cap=4096))
        except serving.BodyRefused as e:
            self.close_connection = True      # what the caller sent beyond the cap stays unread
            return self.send_error(e.status, e.reason)
"""
from __future__ import annotations

import errno
import json
import signal
import threading
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

#: the bytes `read_body` reads when no cap is given: one mebibyte, which holds any JSON a small API takes
DEFAULT_CAP = 1 << 20

#: seconds a stopping server waits for the requests it is answering
DRAIN_S = 10.0


class DrainingMixin:
    """Mix in before ThreadingHTTPServer: counts the requests in progress, so `drain` can wait for
    them. A request is counted from the moment it is accepted, before its thread starts, so one
    accepted just before the stop is waited for too."""

    def __init__(self, *args, **kwargs):
        self._open = 0
        self._idle = threading.Condition()
        super().__init__(*args, **kwargs)

    @property
    def open_requests(self) -> int:
        with self._idle:
            return self._open

    def process_request(self, request, client_address):
        with self._idle:
            self._open += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._done()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._done()

    def _done(self) -> None:
        with self._idle:
            self._open -= 1
            self._idle.notify_all()

    def wait_idle(self, limit: float) -> int:
        """Wait up to `limit` seconds for every request in progress to finish. Returns how many
        were still open when it stopped waiting (0 when all finished)."""
        deadline = time.monotonic() + limit
        with self._idle:
            while self._open > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._idle.wait(left)
            return self._open


def serve_until_stopped(server, *, drain_s: float = DRAIN_S, signals=(signal.SIGTERM,),
                        log: Optional[Callable[[str], None]] = None) -> int:
    """Run `server` (a DrainingMixin server) until one of `signals` or Ctrl-C arrives, then stop
    accepting, wait up to `drain_s` seconds for the open requests and close it. Returns how many
    requests were still open at the end. Call it from the main thread, which owns the signals."""
    say = log or (lambda _m: None)
    stopping = threading.Event()

    def stop(_signum=None, _frame=None):
        if stopping.is_set():
            return
        stopping.set()
        # shutdown() waits for serve_forever's loop, which this thread is running, so it is
        # called from another thread
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {s: signal.signal(s, stop) for s in signals}
    try:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            stop()
        open_now = server.open_requests
        if open_now:
            say(f"stopping: waiting up to {drain_s:g}s for {open_now} open request(s)")
        left = server.wait_idle(drain_s)
        if left:
            say(f"stopped with {left} request(s) still open after {drain_s:g}s")
        server.server_close()
        return left
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def _refused(e: BaseException) -> bool:
    if isinstance(e, ConnectionRefusedError):
        return True
    reason = getattr(e, "reason", None)   # urllib.error.URLError wraps the socket error
    if isinstance(reason, ConnectionRefusedError):
        return True
    return isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED


def retry_refused(call: Callable[[], T], *, tries: int = 4, wait_s: float = 2.0,
                  sleep: Callable[[float], None] = time.sleep) -> T:
    """`call()`, repeated while its connection is refused, up to `tries` times with `wait_s`
    between. Any other error, and the last refusal, is raised as it came."""
    for attempt in range(1, tries + 1):
        try:
            return call()
        except Exception as e:  # noqa: BLE001 - only a refusal is retried
            if attempt == tries or not _refused(e):
                raise
            sleep(wait_s)
    raise AssertionError("unreachable")


class BodyRefused(ValueError):
    """A request body a handler will not read. `status` is the HTTP status to answer with (400 or
    413), and `reason` says why in words that are safe to send back to the caller."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def content_length(value: Optional[str], cap: Optional[int] = DEFAULT_CAP) -> int:
    """The number of body bytes a Content-Length header value announces. A missing or empty value
    is 0. Anything but ASCII digits (a sign, a decimal point, a hexadecimal number, digits of
    another script) raises `BodyRefused(400)`, and a number above `cap` raises `BodyRefused(413)`
    before any byte is read. `cap=None` sets no limit."""
    text = (value or "").strip()
    if not text:
        return 0
    if not (text.isascii() and text.isdigit()):
        raise BodyRefused(400, "Content-Length must be a whole number of bytes")
    # a number of more than 15 digits is over any cap, and int() of a very long one raises
    if len(text) > 15 or (cap is not None and int(text) > cap):
        raise BodyRefused(413, f"the body is over {cap} bytes" if cap is not None else "the body is too large")
    return int(text)


def read_body(handler, cap: Optional[int] = DEFAULT_CAP) -> bytes:
    """The request body of a `BaseHTTPRequestHandler`, at most `cap` bytes (`cap=None` sets no
    limit). Raises `BodyRefused` for a Content-Length that is not a whole number (400), above
    `cap` (413), or announced and not delivered (400). A refusal leaves the body unread on the
    connection, so answer and close it (`handler.close_connection = True`)."""
    n = content_length(handler.headers.get("Content-Length"), cap)
    raw = handler.rfile.read(n) if n else b""
    if len(raw) < n:
        raise BodyRefused(400, "the body ended before its Content-Length")
    return raw


def json_object(raw: bytes) -> dict:
    """`raw` as one JSON object. An empty body is `{}`. A body that is not JSON, or is JSON of
    another shape (a list, a number, null), raises `BodyRefused(400)`."""
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        # a JSONDecodeError, the UnicodeDecodeError of bytes that are not text, and the RecursionError that a body
        # of nested brackets raises (150 KB of "[" is enough, far under the default cap)
        raise BodyRefused(400, "the body is not JSON") from None
    if not isinstance(body, dict):
        raise BodyRefused(400, "the body must be one JSON object")
    return body
