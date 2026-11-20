"""serving: a request in progress when the server is told to stop gets its answer, and a refused
connection is retried while any other failure is not."""
import errno
import json
import os
import signal
import socket
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


# ---- request bodies: the size comes from the caller, so it is checked before anything is read

def test_content_length_reads_only_whole_numbers_within_the_cap():
    assert serving.content_length(None) == 0
    assert serving.content_length("") == 0
    assert serving.content_length("   ") == 0
    assert serving.content_length("0") == 0
    assert serving.content_length(" 12 ") == 12
    assert serving.content_length("64", cap=64) == 64          # the cap itself is allowed
    assert serving.content_length("999999999999", cap=None) == 999999999999
    for bad in ("abc", "-1", "+5", "1.5", "0x10", "1_0", "5 5", "\u0663", "1e3", "NaN"):
        with pytest.raises(serving.BodyRefused) as e:
            serving.content_length(bad, cap=64)
        assert e.value.status == 400, bad
    for big in ("65", "1048577", "9" * 16, "9" * 5000):
        with pytest.raises(serving.BodyRefused) as e:
            serving.content_length(big, cap=64)
        assert e.value.status == 413, big
    with pytest.raises(serving.BodyRefused) as e:             # with no cap a number of 16 digits is still refused
        serving.content_length("9" * 16, cap=None)
    assert e.value.status == 413
    assert serving.content_length(str(serving.DEFAULT_CAP)) == serving.DEFAULT_CAP
    with pytest.raises(serving.BodyRefused):
        serving.content_length(str(serving.DEFAULT_CAP + 1))   # a cap left out is the default one


def test_json_object_is_one_object_or_a_400():
    assert serving.json_object(b"") == {}
    assert serving.json_object(b"  \n") == {}
    assert serving.json_object(b'{"a": 1}') == {"a": 1}
    for bad in (b"{", b"not json", b"\xff\xfe", b"[1, 2]", b"7", b"null", b'"text"'):
        with pytest.raises(serving.BodyRefused) as e:
            serving.json_object(bad)
        assert e.value.status == 400, bad
    # nested brackets deep enough raise RecursionError in json.loads, which is not a ValueError, and 150 KB is well under the default cap
    for deep in (b"[" * 150_000, b"[" * 200_000 + b"]" * 200_000):
        assert len(deep) < serving.DEFAULT_CAP
        with pytest.raises(serving.BodyRefused) as e:
            serving.json_object(deep)
        assert e.value.status == 400 and e.value.reason == "the body is not JSON"
    assert issubclass(serving.BodyRefused, ValueError)


class Echo(BaseHTTPRequestHandler):
    """Answers the JSON object it was posted, with a cap of 64 bytes."""
    seen = []

    def do_POST(self):
        try:
            body = serving.json_object(serving.read_body(self, cap=64))
        except serving.BodyRefused as e:
            self.close_connection = True
            out, status = json.dumps({"error": e.reason}).encode(), e.status
        else:
            out, status = json.dumps({"got": body}).encode(), 200
        Echo.seen.append(status)
        self.send_response(status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def raw_post(port, headers, body=b"", half_close=False):
    """One POST with the header lines given exactly as they are (http.client would fix a bad
    Content-Length), the status and the JSON answer."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(("POST / HTTP/1.1\r\nHost: example.org\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n").encode() + body)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        want = int(next(h.split(b":")[1] for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")))
        while len(rest) < want:
            chunk = s.recv(4096)
            if not chunk:
                break
            rest += chunk
        return int(head.split()[1]), json.loads(rest)


def test_a_handler_answers_a_refused_body_with_its_status():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ok = b'{"a": 1}'
        assert raw_post(port, [f"Content-Length: {len(ok)}"], ok) == (200, {"got": {"a": 1}})
        assert raw_post(port, ["Content-Length: 0"]) == (200, {"got": {}})
        assert raw_post(port, []) == (200, {"got": {}})                       # no header is no body
        for bad in ("abc", "-5", "1.5", "0x10", ""):
            status, out = raw_post(port, [f"Content-Length: {bad}"], b'{"a": 1}')
            if bad == "":
                assert status == 200, "an empty Content-Length is no body"
            else:
                assert status == 400 and "whole number" in out["error"], bad
        over = b'{"pad": "' + b"x" * 100 + b'"}'
        status, out = raw_post(port, [f"Content-Length: {len(over)}"], over)
        assert (status, out["error"]) == (413, "the body is over 64 bytes")  # the answer reaches a caller still sending
        status, out = raw_post(port, ["Content-Length: " + "9" * 5000])
        assert status == 413
        status, out = raw_post(port, ["Content-Length: 7"], b"{not js")
        assert (status, out["error"]) == (400, "the body is not JSON")
        status, out = raw_post(port, ["Content-Length: 4"], b"[12]")
        assert (status, out["error"]) == (400, "the body must be one JSON object")
        status, out = raw_post(port, ["Content-Length: 50"], b'{"a"', half_close=True)
        assert (status, out["error"]) == (400, "the body ended before its Content-Length")
        assert raw_post(port, [f"Content-Length: {len(ok)}"], ok)[0] == 200   # and the server still answers
    finally:
        srv.shutdown()
