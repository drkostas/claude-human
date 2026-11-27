"""serving: a request in progress when the server is told to stop gets its answer, and a refused
connection is retried while any other failure is not."""
import errno
import json
import os
import re
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
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


# ---------------------------------------------------------------------------------------
# running and watch_running: a request that does not end is named while it waits
# ---------------------------------------------------------------------------------------

class Held(BaseHTTPRequestHandler):
    gate = threading.Event()

    def do_GET(self):
        if self.path == "/hold-labelled":
            Held.labelled = [serving.label("db pid", 4242), serving.label("db pid", 4243),
                             serving.label("step", "reading")]
            Held.gate.wait(10)
        if self.path == "/hold":
            Held.gate.wait(10)
        if self.path == "/boom":
            raise RuntimeError("the handler failed")
        body = b"done"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def log_error(self, *a):
        pass


def until(test, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        got = test()
        if got:
            return got
        time.sleep(0.02)
    return test()


@pytest.fixture
def held():
    Held.gate = threading.Event()
    srv = Server(("127.0.0.1", 0), Held)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    Held.gate.set()
    srv.shutdown()
    srv.server_close()


def ask_in_background(port, path):
    got = {}

    def ask():
        try:
            got["answer"] = get(port, path)
        except Exception as e:   # noqa: BLE001
            got["error"] = e

    t = threading.Thread(target=ask)
    t.start()
    return t, got


def test_running_names_a_waiting_request_by_its_line_and_stack(held):
    port = held.server_address[1]
    asker, got = ask_in_background(port, "/hold")
    rows = until(lambda: held.running())
    assert len(rows) == 1
    r = rows[0]
    assert r["request"] == "GET /hold HTTP/1.1"
    assert r["seconds"] >= 0 and r["client"][0] == "127.0.0.1"
    # the stack names the handler and the line it waits on
    assert "do_GET" in r["stack"] and "Held.gate.wait(10)" in r["stack"]
    assert held.running(older_than=60) == []
    Held.gate.set()
    asker.join(5)
    assert got["answer"] == (200, b"done")
    assert until(lambda: held.running() == [])
    assert held.open_requests == 0


def test_a_request_whose_handler_raised_leaves_the_list(held):
    port = held.server_address[1]
    with pytest.raises(Exception):
        get(port, "/boom")
    assert until(lambda: held.running() == [] and held.open_requests == 0)


def test_watch_running_logs_a_long_request_once_and_then_its_end(held):
    port = held.server_address[1]
    logs = []
    stop = serving.watch_running(held, after_s=0.2, log=logs.append, every_s=0.05)
    try:
        asker, got = ask_in_background(port, "/hold")
        assert until(lambda: [m for m in logs if "still running" in m])
        time.sleep(0.3)   # several more looks while it still waits
        waiting = [m for m in logs if "still running" in m]
        assert len(waiting) == 1
        assert "GET /hold HTTP/1.1" in waiting[0] and "Held.gate.wait(10)" in waiting[0]
        assert not [m for m in logs if "has ended" in m]
        Held.gate.set()
        asker.join(5)
        ended = until(lambda: [m for m in logs if "has ended" in m])
        assert len(ended) == 1 and "GET /hold HTTP/1.1" in ended[0]
    finally:
        stop.set()


def test_watch_running_only_logs_and_the_request_it_named_still_answers_in_full(held):
    port = held.server_address[1]
    logs = []
    stop = serving.watch_running(held, after_s=0.1, log=logs.append, every_s=0.02)
    try:
        asker, got = ask_in_background(port, "/hold")
        assert until(lambda: [m for m in logs if "still running" in m])
        time.sleep(0.3)   # many more looks after the one that logged it
        assert asker.is_alive() and got == {}
        assert [r["request"] for r in held.running()] == ["GET /hold HTTP/1.1"]
        Held.gate.set()
        asker.join(5)
        assert got == {"answer": (200, b"done")}
        assert get(port, "/after") == (200, b"done")
    finally:
        stop.set()


def test_watch_running_says_nothing_of_a_quick_request(held):
    port = held.server_address[1]
    logs = []
    stop = serving.watch_running(held, after_s=0.5, log=logs.append, every_s=0.05)
    try:
        assert get(port, "/quick") == (200, b"done")
        time.sleep(0.8)
        assert logs == []
    finally:
        stop.set()


def test_watch_running_goes_on_after_a_look_that_failed(held):
    logs = []
    real = held.running
    calls = {"n": 0}

    def once_broken(older_than=0.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no frames")
        return real(older_than)

    held.running = once_broken
    stop = serving.watch_running(held, after_s=0.1, log=logs.append, every_s=0.05)
    try:
        assert until(lambda: calls["n"] >= 3)
        assert [m for m in logs if "one look failed: RuntimeError: no frames" in m]
    finally:
        stop.set()


# ---------------------------------------------------------------------------------------
# the join: a watch line carries the request's wall-clock start, the time of the look and the request's own labels
# ---------------------------------------------------------------------------------------

def test_a_label_belongs_to_the_request_its_thread_answers(held):
    port = held.server_address[1]
    assert serving.label("db pid", 1) is False   # this thread answers no request, so nothing is attached
    before = time.time()
    asker, got = ask_in_background(port, "/hold-labelled")
    rows = until(lambda: [r for r in held.running() if r["labels"]])
    assert len(rows) == 1
    r = rows[0]
    assert Held.labelled == [True, True, True]
    assert r["labels"] == {"db pid": 4243, "step": "reading"}   # a second call with a key replaces its value
    assert before <= r["started_at"] <= time.time()
    r["labels"]["db pid"] = 0                                 # running() hands out a copy
    assert held.running()[0]["labels"]["db pid"] == 4243
    Held.gate.set()
    asker.join(5)
    assert got["answer"] == (200, b"done")
    assert until(lambda: held.running() == [])
    # the next request starts with no labels, whichever thread id it is given
    Held.gate = threading.Event()
    asker, got = ask_in_background(port, "/hold")
    rows = until(lambda: held.running())
    assert rows[0]["labels"] == {}
    Held.gate.set()
    asker.join(5)
    assert serving._labels == {} or until(lambda: serving._labels == {})


def test_watch_running_prints_the_start_the_look_and_the_labels(held):
    port = held.server_address[1]
    logs = []
    before = time.time()
    stop = serving.watch_running(held, after_s=0.2, log=logs.append, every_s=0.05)
    try:
        asker, got = ask_in_background(port, "/hold-labelled")
        waiting = until(lambda: [m for m in logs if "still running" in m])
        assert len(waiting) == 1
        line = waiting[0].splitlines()[0]
        m = re.search(r"\(started (\S+), seen (\S+)\): GET /hold-labelled HTTP/1.1 from .*, thread \d+, "
                      r"db pid 4243, step reading, waiting in$", line)
        assert m, line
        started, seen = (datetime.fromisoformat(x).timestamp() for x in m.groups())
        assert before - 0.01 <= started <= seen <= time.time() + 0.01
        assert seen - started >= 0.2                      # the look came after the bound, and says when
        Held.gate.set()
        asker.join(5)
        ended = until(lambda: [m for m in logs if "has ended" in m])
        assert len(ended) == 1
        assert f"(started {m.group(1)}): GET /hold-labelled HTTP/1.1, thread " in ended[0]
        assert ended[0].endswith(", db pid 4243, step reading")
    finally:
        stop.set()


def test_wall_prints_local_time_with_milliseconds_and_its_offset():
    t = 1767316245.678
    text = serving.wall(t)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d", text), text
    assert abs(datetime.fromisoformat(text).timestamp() - t) < 0.001
