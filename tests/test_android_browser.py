"""android_browser: the phone browser's debugging socket is found, forwarded and spoken to."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_human import android_browser as ab

PROC = """Num       RefCount Protocol Flags    Type St Inode Path
0000000000000000: 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote
0000000000000000: 00000002 00000000 00010000 0001 01 12346 @webview_devtools_remote_4242
0000000000000000: 00000002 00000000 00010000 0001 01 12347 /dev/socket/zygote
"""


def test_the_socket_is_the_packages_own_or_chromes():
    assert ab.socket_name(PROC, "com.android.chrome") == "chrome_devtools_remote"
    own = PROC + "0000000000000000: 00000002 00000000 00010000 0001 01 1 @org.example.browser_devtools_remote\n"
    assert ab.socket_name(own, "org.example.browser") == "org.example.browser_devtools_remote"
    assert ab.socket_name("nothing here", "com.android.chrome") is None


def test_the_tab_is_the_first_page_at_the_address():
    listed = [{"type": "service_worker", "url": "http://127.0.0.1:8000/sw.js"},
              {"type": "page", "url": "https://example.org/other"},
              {"type": "page", "url": "http://127.0.0.1:8000/page.html#x"}]
    assert ab.tab_for(listed, "http://127.0.0.1:8000/page")["url"].endswith("#x")
    assert ab.tab_for(listed, "")["url"] == "https://example.org/other"
    assert ab.tab_for(listed, "https://nowhere") is None


def test_a_client_frame_is_masked_in_each_length_form():
    mask = b"\x01\x02\x03\x04"
    for n, head in ((5, 2), (200, 4), (70000, 10)):
        frame = ab._frame("a" * n, mask=mask)
        assert frame[0] == 0x81 and frame[1] & 0x80
        assert frame[head:head + 4] == mask and len(frame) == head + 4 + n
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[head + 4:]))
        assert body == b"a" * n


class _Pipe:
    def __init__(self, data: bytes):
        self.data = data

    def recv(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


def _server_frame(text: str) -> bytes:
    data = text.encode()
    n = len(data)
    if n < 126:
        return bytes([0x81, n]) + data
    if n < 1 << 16:
        return bytes([0x81, 126]) + struct.pack(">H", n) + data
    return bytes([0x81, 127]) + struct.pack(">Q", n) + data


def test_server_frames_of_each_length_are_read():
    for n in (3, 300, 70000):
        assert ab._read_frame(_Pipe(_server_frame("b" * n))) == (1, b"b" * n)
    # a message split over two frames is joined
    split = bytes([0x01, 2]) + b"ab" + bytes([0x80, 2]) + b"cd"
    assert ab._read_frame(_Pipe(split)) == (1, b"abcd")


class FakeBrowser:
    """A browser's debugging endpoint on loopback: /json/version, /json/list and one websocket
    target that answers Runtime.evaluate with `answer`."""

    def __init__(self, answer, package="com.android.chrome"):
        self.answer, self.package, self.asked = answer, package, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/json/version":
                    return self._json({"Android-Package": outer.package, "Browser": "Chrome/1"})
                if self.path == "/json/list":
                    return self._json([{"type": "page", "url": "http://127.0.0.1:1/page.html",
                                        "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/AB"}])
                if self.path == "/devtools/page/AB":
                    key = self.headers["Sec-WebSocket-Key"]
                    accept = base64.b64encode(hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.wfile.flush()
                    op, data = ab._read_frame(self.connection)
                    msg = json.loads(data)
                    outer.asked.append(msg)
                    self.connection.sendall(_server_frame(json.dumps({"id": msg["id"], "result": outer.answer})))
                    return
                self.send_error(404)

            def _json(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def test_evaluate_runs_the_script_in_the_tab_and_returns_its_value():
    b = FakeBrowser({"result": {"type": "object", "value": {"level": 2}}})
    try:
        assert ab.evaluate(b.port, "http://127.0.0.1:1/page", "({level: 2})") == {"level": 2}
        sent = b.asked[0]
        assert sent["method"] == "Runtime.evaluate" and sent["params"]["returnByValue"] is True
        with pytest.raises(ab.BrowserError, match="no tab"):
            ab.evaluate(b.port, "https://elsewhere", "1")
    finally:
        b.close()


def test_a_script_that_throws_is_an_error_with_its_text():
    b = FakeBrowser({"result": {}, "exceptionDetails": {"exception": {"description": "ReferenceError: x"}}})
    try:
        with pytest.raises(ab.BrowserError, match="ReferenceError: x"):
            ab.evaluate(b.port, "", "x")
    finally:
        b.close()


class AdbFake:
    def __init__(self, port, proc=PROC):
        self.port, self.proc, self.calls = port, proc, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[2:] == ["shell", ab.PROC_UNIX]:
            return 0, self.proc
        if argv[2:4] == ["forward", "tcp:0"]:
            return 0, f"{self.port}\n"
        return 0, ""


def test_forward_checks_the_socket_belongs_to_the_package_and_is_removed_after():
    b = FakeBrowser({"result": {"value": 1}}, package="com.brave.browser")
    try:
        run = AdbFake(b.port)
        with ab.forwarded("dev", "com.brave.browser", run) as port:
            assert port == b.port
        assert ["-s", "dev", "forward", "--remove", f"tcp:{b.port}"] in run.calls
        run = AdbFake(b.port)
        with pytest.raises(ab.BrowserError, match="belongs to com.brave.browser"):
            ab.forward("dev", "com.android.chrome", run)
        assert ["-s", "dev", "forward", "--remove", f"tcp:{b.port}"] in run.calls
        with pytest.raises(ab.BrowserError, match="no debugging socket"):
            ab.forward("dev", "com.android.chrome", AdbFake(b.port, proc="nothing"))
    finally:
        b.close()


def test_open_url_quotes_the_address_for_the_phones_shell():
    run = AdbFake(0)
    assert ab.open_url("dev", "com.android.chrome", "http://127.0.0.1:8000/p.html?a=1&b=2", run)
    assert run.calls[-1][-3:] == ["-d", "'http://127.0.0.1:8000/p.html?a=1&b=2'", "com.android.chrome"]


def test_the_command_line_prints_the_value_as_json(capsys):
    b = FakeBrowser({"result": {"value": [1, "two"]}})
    try:
        rc = ab.main(["--serial", "dev", "--package", "com.android.chrome", "--eval", "[1,'two']"],
                     run=AdbFake(b.port))
        assert rc == 0 and json.loads(capsys.readouterr().out) == [1, "two"]
    finally:
        b.close()
