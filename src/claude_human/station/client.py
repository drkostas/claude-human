"""A client for a running station, for a program that has no Screen Recording or Accessibility grant
of its own.

The station holds both grants, so a program can ask it for a picture of one window and send input to
that window instead. Every call takes the station's address and a token (the master token chooses
any window; a pinned grant reaches only its own).

- `shot(window)` is a JPEG of the window with its width and height in pixels, or None.
- `click(window, x, y)` presses at a point given as fractions of the window, and `scroll`, `key` and
  `text` send the other inputs the station takes. Each returns the station's answer, which says
  {"ok": False, "detail"} when it refused (the screen is locked, a dialog is up, another window
  covers that point), or {"ok": False, "detail"} when the station could not be asked.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

#: where a station listens by default
STATION = "http://127.0.0.1:8789"


def _req(station: str, path: str, token: str, body: Optional[dict] = None) -> urllib.request.Request:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(f"{station.rstrip('/')}{path}", data=data, headers=headers,
                                  method="POST" if data is not None else "GET")


def send(msg: dict, *, token: str, station: str = STATION, timeout: float = 20.0,
         opener: Callable = urllib.request.urlopen) -> dict:
    """One input (a dict the station's /input takes) and the station's answer."""
    try:
        with opener(_req(station, "/input", token, msg), timeout=timeout) as r:
            got = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read() or b"{}")
        except (OSError, ValueError):
            got = None
        if isinstance(got, dict) and got:
            return {"ok": False, **got}
        return {"ok": False, "detail": f"the station answered {e.code}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "detail": f"the station did not answer: {type(e).__name__}"}
    return got if isinstance(got, dict) else {"ok": False, "detail": "the station's answer was not an object"}


def shot(window: int, *, token: str, station: str = STATION, full: bool = False, timeout: float = 20.0,
         opener: Callable = urllib.request.urlopen) -> Optional[tuple[bytes, int, int]]:
    """A JPEG of `window` and its size in pixels, or None when the station has nothing to capture
    or could not be asked. `full` asks for the window's own size at a high quality, for reading
    text from it (the ordinary shot is made small for streaming)."""
    q = urllib.parse.urlencode({"window": int(window), **({"full": 1} if full else {})})
    try:
        with opener(_req(station, f"/shot?{q}", token), timeout=timeout) as r:
            body = r.read()
            size = (r.headers.get("X-Frame-Size") or "0x0").lower().split("x")
    except (urllib.error.HTTPError, OSError, ValueError):
        return None
    try:
        w, h = int(size[0]), int(size[1])
    except (IndexError, ValueError):
        w = h = 0
    return (body, w, h) if body else None


def click(window: int, x: float, y: float, *, double: bool = False, **kw) -> dict:
    """A left click at (x, y), fractions of `window` from its top left."""
    return send({"type": "dblclick" if double else "click", "window": int(window), "x": float(x), "y": float(y)}, **kw)


def scroll(window: int, dy: int, *, x: float = 0.5, y: float = 0.5, dx: int = 0, **kw) -> dict:
    """A scroll of `dy` pixels (positive is up) with the pointer at (x, y) in `window`."""
    return send({"type": "scroll", "window": int(window), "dy": int(dy), "dx": int(dx), "x": float(x), "y": float(y)}, **kw)


def key(window: int, code: int, flags: int = 0, **kw) -> dict:
    """One key press, a virtual key code and modifier flags, for `window`."""
    return send({"type": "key", "window": int(window), "code": int(code), "flags": int(flags)}, **kw)


def text(window: int, words: str, **kw) -> dict:
    """Text put into `window` (the station pastes it), at most 2000 characters."""
    return send({"type": "text", "window": int(window), "text": str(words)}, **kw)


def windows(*, token: str, station: str = STATION, timeout: float = 20.0,
            opener: Callable = urllib.request.urlopen) -> Optional[list[dict]]:
    """The windows the station can show (a master token only), as {"id", "app", "title", "x", "y",
    "width", "height"}, or None when it could not be asked or refused."""
    try:
        with opener(_req(station, "/windows", token), timeout=timeout) as r:
            got = json.loads(r.read() or b"{}")
    except (urllib.error.HTTPError, OSError, ValueError):
        return None
    listed = got.get("windows") if isinstance(got, dict) else None
    return listed if isinstance(listed, list) else None
