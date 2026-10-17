"""serving: a request in progress when the server is told to stop gets its answer, and a refused
connection is retried while any other failure is not."""
import errno
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_human import serving


class Slow(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(1.0)
        body = b"done"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Server(serving.DrainingMixin, ThreadingHTTPServer):
    daemon_threads = True


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, r.read()


def test_sigterm_lets_the_open_request_finish():
    srv = Server(("127.0.0.1", 0), Slow)
    port = srv.server_address[1]
    got = {}

    def ask():
        got["answer"] = get(port, "/slow")

    # serve_until_stopped owns the signal, so the server runs in this (the main) thread and the
    # request and the SIGTERM come from others; the SIGTERM lands while /slow is being answered
    asker = threading.Thread(target=ask)
    asker.start()
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    left = serving.serve_until_stopped(srv, drain_s=5)
    asker.join(5)
    assert got["answer"] == (200, b"done")
    assert left == 0
    with pytest.raises(urllib.error.URLError):
        get(port, "/")


def test_a_drain_that_runs_out_says_how_many_were_left():
    srv = Server(("127.0.0.1", 0), Slow)
    port = srv.server_address[1]
    threading.Thread(target=lambda: _quiet(lambda: get(port, "/slow")), daemon=True).start()
    said = []
    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    left = serving.serve_until_stopped(srv, drain_s=0.1, log=said.append)
    assert left == 1 and any("still open" in s for s in said)


def _quiet(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - the server was closed under it on purpose
        pass


def test_the_previous_handler_is_put_back():
    before = signal.getsignal(signal.SIGTERM)
    srv = Server(("127.0.0.1", 0), Slow)
    threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    serving.serve_until_stopped(srv, drain_s=1)
    assert signal.getsignal(signal.SIGTERM) == before


def test_retry_refused_repeats_only_a_refusal():
    calls, slept = [], []

    def refused_twice():
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
        return "ok"

    assert serving.retry_refused(refused_twice, sleep=slept.append) == "ok"
    assert len(calls) == 3 and slept == [2.0, 2.0]

    def lost():
        raise ConnectionResetError("reset by peer")

    with pytest.raises(ConnectionResetError):
        serving.retry_refused(lost, sleep=slept.append)
    assert len(slept) == 2, "a request that got in is never repeated"

    def always():
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    with pytest.raises(ConnectionRefusedError):
        serving.retry_refused(always, tries=3, sleep=lambda s: None)
