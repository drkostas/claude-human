"""Read a page in a Chromium browser on an Android phone, over adb, through the browser's debugging socket.

Chrome and the browsers built on it (Brave, Edge, ...) listen on an abstract Unix socket on the phone
(`chrome_devtools_remote`, or `<package>_devtools_remote`) and speak Chrome's DevTools protocol on it
once USB debugging is on. `adb forward` brings that socket to a local port, and from there a script
can list the tabs and run JavaScript in one of them: what level a page shows, where an element sits,
the browser's own zoom. A test that acts on the phone (a tap, a pinch) reads its result this way.

`open_url` opens an address in the browser in front, with `am start` (never `monkey`, which also
injects a random event). `evaluate` runs a script in the tab showing an address and returns its value.
`pinch` makes a two-finger pinch in that tab with the browser's own synthetic gesture, which works on
a phone whose security policy refuses the shell a write to the touch screen (see android_touch).
The package has no dependencies, so the websocket the protocol needs is a small client of its own.

    claude-human android browser --serial HOST:PORT --package com.android.chrome --open https://example.org
    claude-human android browser --serial HOST:PORT --package com.android.chrome --url https://example.org --eval "document.title"
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import socket
import struct
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from urllib.parse import urlsplit

from claude_human import android

PROC_UNIX = "cat /proc/net/unix"
#: seconds a reply from the browser may take
TIMEOUT = 20.0


class BrowserError(RuntimeError):
    """The browser could not be reached, had no such tab, or the script threw."""


def socket_name(proc_net_unix: str, package: str) -> str | None:
    """Pure. The debugging socket of `package`: its own `<package>_devtools_remote` when it has one,
    else `chrome_devtools_remote`, which the Chromium browser in front holds. None when neither is
    listed."""
    names = set(re.findall(r"@([\w.]*devtools_remote[\w.]*)", proc_net_unix or ""))
    for name in (f"{package}_devtools_remote", "chrome_devtools_remote"):
        if name in names:
            return name
    return None


def tab_for(tabs: list[dict], url: str) -> dict | None:
    """Pure. The first page tab whose address starts with `url` (any page tab when `url` is empty)."""
    for t in tabs:
        if t.get("type", "page") == "page" and (t.get("url") or "").startswith(url or ""):
            return t
    return None


def forward(serial: str, package: str, run: android.Runner) -> int:
    """A local port forwarded to `package`'s debugging socket. Raises BrowserError when the phone
    lists none, or the socket belongs to another browser."""
    name = socket_name(android._read(serial, run, PROC_UNIX) or "", package)
    if name is None:
        raise BrowserError(f"no debugging socket on the phone for {package} (is it running?)")
    code, out = run(["-s", serial, "forward", "tcp:0", f"localabstract:{name}"])
    if code != 0 or not out.strip().isdigit():
        raise BrowserError(f"adb could not forward {name}: {out.strip()[:200]}")
    port = int(out.strip())
    owner = _get(port, "/json/version").get("Android-Package")
    if owner and owner != package:
        unforward(serial, port, run)
        raise BrowserError(f"{name} belongs to {owner}, not {package}")
    return port


def unforward(serial: str, port: int, run: android.Runner) -> None:
    run(["-s", serial, "forward", "--remove", f"tcp:{port}"])


@contextmanager
def forwarded(serial: str, package: str, run: android.Runner):
    port = forward(serial, package, run)
    try:
        yield port
    finally:
        unforward(serial, port, run)


def open_url(serial: str, package: str, url: str, run: android.Runner) -> bool:
    """Open `url` in `package` in front of everything else."""
    code, _ = run(["-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                   "-d", shlex.quote(url), package])
    return code == 0


def _get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError) as e:
        raise BrowserError(f"the browser did not answer {path}: {e}") from None


def tabs(port: int) -> list[dict]:
    return _get(port, "/json/list")


# --- a websocket client, enough for the DevTools protocol (RFC 6455, text frames) ---------------

def _frame(text: str, *, mask: bytes | None = None) -> bytes:
    """Pure. One masked text frame with FIN set, as a client must send it."""
    data = text.encode()
    mask = mask if mask is not None else os.urandom(4)
    n = len(data)
    head = bytes([0x81])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _exact(sock, n: int) -> bytes:
    out = b""
    while len(out) < n:
        part = sock.recv(n - len(out))
        if not part:
            raise BrowserError("the browser closed the connection")
        out += part
    return out


def _read_frame(sock) -> tuple[int, bytes]:
    """(opcode, payload) of the next frame; a payload split over frames is joined."""
    payload, opcode = b"", None
    while True:
        b0, b1 = _exact(sock, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _exact(sock, 8))[0]
        mask = _exact(sock, 4) if b1 & 0x80 else None
        data = _exact(sock, n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        opcode = opcode if opcode is not None and (b0 & 0x0F) == 0 else b0 & 0x0F
        payload += data
        if b0 & 0x80:
            return opcode, payload


def _connect(ws_url: str, port: int):
    """A websocket to the path of `ws_url`, on our forwarded port (the browser names its own)."""
    path = urlsplit(ws_url).path
    sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        part = sock.recv(1024)
        if not part:
            raise BrowserError("the browser closed the connection during the handshake")
        head += part
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        sock.close()
        raise BrowserError(f"the browser refused the websocket: {status.decode(errors='replace')}")
    return sock


def call(port: int, ws_url: str, method: str, params: dict | None = None) -> dict:
    """One DevTools call on the target at `ws_url`, and its result."""
    sock = _connect(ws_url, port)
    try:
        sock.sendall(_frame(json.dumps({"id": 1, "method": method, "params": params or {}})))
        while True:
            opcode, data = _read_frame(sock)
            if opcode == 8:
                raise BrowserError("the browser closed the connection")
            if opcode != 1:
                continue
            msg = json.loads(data.decode())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise BrowserError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
    finally:
        sock.close()


def evaluate(port: int, url: str, js: str):
    """The value of `js` (awaited when it is a promise) in the tab showing `url`."""
    tab = tab_for(tabs(port), url)
    if tab is None:
        raise BrowserError(f"no tab shows {url or 'a page'}")
    result = call(port, tab["webSocketDebuggerUrl"], "Runtime.evaluate",
                  {"expression": js, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in result:
        d = result["exceptionDetails"]
        raise BrowserError(f"the script threw: {(d.get('exception') or {}).get('description') or d.get('text')}")
    return (result.get("result") or {}).get("value")


def pinch(port: int, url: str, x: float, y: float, scale: float, *, speed: float = 800.0) -> None:
    """A two-finger pinch around (x, y), in the page's CSS pixels, in the tab showing `url`: the
    fingers end at `scale` times their distance (below 1 together, above 1 apart), moving `speed`
    pixels a second. It is the browser's own synthetic gesture (Input.synthesizePinchGesture), which on
    Android feeds touch events into the browser's view, so the page's touch handlers and the browser's
    own zoom both see two fingers. It returns once the gesture has finished."""
    if scale <= 0:
        raise ValueError(f"a pinch scales by a positive factor, not {scale!r}")
    tab = tab_for(tabs(port), url)
    if tab is None:
        raise BrowserError(f"no tab shows {url or 'a page'}")
    call(port, tab["webSocketDebuggerUrl"], "Input.synthesizePinchGesture",
         {"x": x, "y": y, "scaleFactor": scale, "relativeSpeed": int(speed), "gestureSourceType": "touch"})


def main(argv: list[str], *, run: android.Runner | None = None,
         env: Mapping[str, str] | None = None) -> int:
    import argparse   # noqa: PLC0415
    p = argparse.ArgumentParser(prog="claude-human android browser")
    p.add_argument("--serial", required=True, help="the device, as adb names it")
    p.add_argument("--package", required=True, help="the browser's package name")
    p.add_argument("--open", help="open this address in the browser, in front")
    p.add_argument("--url", default="", help="the tab to run --eval in: the first whose address starts with this")
    p.add_argument("--eval", dest="js", help="run this JavaScript in the tab and print its value as JSON")
    p.add_argument("--adb", default=android.ADB)
    a = p.parse_args(argv)
    run = run or android._runner(a.adb, env)
    if a.open and not open_url(a.serial, a.package, a.open, run):
        print(f"REFUSED: could not open {a.open} in {a.package}")
        return 2
    if a.js is not None:
        try:
            with forwarded(a.serial, a.package, run) as port:
                print(json.dumps(evaluate(port, a.url, a.js), ensure_ascii=False))
        except BrowserError as e:
            print(f"REFUSED: {e}")
            return 2
    return 0
