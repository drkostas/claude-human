"""What is on a Mac's screen, and its pixels.

This module lists the windows a person works in, finds a window by application name, captures the
screen or one window as JPEG, and answers three questions about the screen state: is the session
locked, is a system dialog holding focus, and is a password panel (SecurityAgent) on screen.

Capture runs the ``sckshot`` helper (ScreenCaptureKit) and falls back to CGWindowListCreateImage,
which is throttled on recent macOS. Quartz comes from ``pyobjc-framework-Quartz`` (the ``macos``
extra) and is imported only when a function needs it, so the pure helpers work on any platform.

Screen Recording must be granted to whatever captures: to sckshot.app for the fast path, and to the
Python process (or the terminal or launchd job that runs it) for window titles and the fallback.
Without the grant macOS returns windows with empty titles, which is the sign to look for.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Any, Iterable

from . import paths

DEFAULT_MAX_WIDTH = 1100
DEFAULT_QUALITY = 0.55

#: Windows smaller than this are palettes, menus or helpers, not windows a person works in.
MIN_WINDOW = (200, 150)

#: System processes whose dialogs take exclusive focus. While one is up, synthetic clicks are
#: accepted and delivered nowhere.
BLOCKING_APPS = {"universalAccessAuthWarn", "UserNotificationCenter",
                 "SecurityAgent", "CoreServicesUIAgent"}

#: The owner of macOS credential panels.
AUTH_OWNERS = {"SecurityAgent"}

#: A credential panel is at least this size (SecurityAgent also keeps tiny offscreen helper windows)
#: and at most this fraction of the display (the lock screen is a full-display surface).
AUTH_MIN = 160
AUTH_MAX_FRACTION = 0.7


def _quartz():
    import Quartz  # noqa: PLC0415 - macOS only, see the module docstring
    return Quartz


def _raw_windows() -> list[dict]:
    q = _quartz()
    opts = q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements
    return list(q.CGWindowListCopyWindowInfo(opts, q.kCGNullWindowID) or [])


# ----------------------------------------------------------------------------- pure helpers

def to_windows(raw: Iterable[dict], min_size: tuple[int, int] = MIN_WINDOW) -> list[dict]:
    """Turn CGWindowListCopyWindowInfo entries into plain dicts, keeping only ordinary windows.

    Only layer 0 is kept (the normal application layer). Menu bar items, the Dock and the wallpaper
    live on other layers. The result is sorted by application and title."""
    out = []
    for w in raw:
        b = w.get("kCGWindowBounds") or {}
        width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
        if width < min_size[0] or height < min_size[1]:
            continue
        if int(w.get("kCGWindowLayer", 0)) != 0:
            continue
        out.append({
            "id": int(w.get("kCGWindowNumber")),
            "app": str(w.get("kCGWindowOwnerName") or ""),
            "title": str(w.get("kCGWindowName") or ""),  # empty without Screen Recording
            "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
            "width": width, "height": height,
        })
    out.sort(key=lambda w: (w["app"].lower(), w["title"].lower()))
    return out


def pick_window(wins: list[dict], app: str) -> int | None:
    """The id of the window that best matches an application name, or None.

    An exact (case-insensitive) name wins over a substring match, and among matches the largest
    window wins, which is the document window rather than a palette or an alert."""
    want = app.lower()
    matches = [w for w in wins if w["app"].lower() == want]
    if not matches:
        matches = [w for w in wins if want in w["app"].lower()]
    if not matches:
        return None
    return max(matches, key=lambda w: w["width"] * w["height"])["id"]


def find_blocker(raw: Iterable[dict], blocking: set[str] = BLOCKING_APPS) -> str | None:
    """The owner name of the first window, on any layer, that belongs to a blocking system app."""
    for w in raw:
        owner = str(w.get("kCGWindowOwnerName") or "")
        if owner in blocking:
            return owner
    return None


def find_auth_panel(raw: Iterable[dict], display: tuple[float, float],
                    owners: set[str] = AUTH_OWNERS) -> dict | None:
    """The first visible credential panel among windows listed front to back, or None.

    ``display`` is the main display size in points. A panel is a window of an auth owner, not fully
    transparent, larger than the helper windows and smaller than the lock screen surface."""
    cap_w, cap_h = display[0] * AUTH_MAX_FRACTION, display[1] * AUTH_MAX_FRACTION
    for w in raw:
        if str(w.get("kCGWindowOwnerName") or "") not in owners:
            continue
        if float(w.get("kCGWindowAlpha", 1)) <= 0:
            continue
        b = w.get("kCGWindowBounds", {}) or {}
        ww, hh = float(b.get("Width", 0)), float(b.get("Height", 0))
        if ww < AUTH_MIN or hh < AUTH_MIN or ww > cap_w or hh > cap_h:
            continue
        return {"wid": int(w.get("kCGWindowNumber")),
                "owner": str(w.get("kCGWindowOwnerName") or ""),
                "title": str(w.get("kCGWindowName") or "")}
    return None


def sckshot_args(binary: str | os.PathLike, out: str, wid: int | None = None,
                 max_width: int = 0) -> list[str]:
    """The command line for one sckshot capture."""
    args = [str(binary), "--out", str(out), "--max-width", str(int(max_width))]
    if wid:
        args += ["--window", str(int(wid))]
    return args


def parse_sckshot_output(stdout: bytes | str) -> tuple[int, int] | None:
    """The image size from sckshot's "OK <w>x<h>" line, or None."""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    m = re.match(rb"OK (\d+)x(\d+)", stdout.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


# ----------------------------------------------------------------------------- windows

def windows() -> list[dict]:
    """Every ordinary on-screen window: ``{id, app, title, x, y, width, height}``."""
    return to_windows(_raw_windows())


def resolve(app: str | None, wid: int | None) -> int | None:
    """Which window a request means right now.

    A CGWindowID changes every time an application restarts, so storing one and using it later can
    reach a different window. Pass the application name and the id is looked up again on every
    call. With no application name, ``wid`` is returned as it is."""
    if app:
        return pick_window(windows(), app)
    return wid


def window_rect(wid: int):
    for w in windows():
        if w["id"] == wid:
            return _quartz().CGRectMake(w["x"], w["y"], w["width"], w["height"])
    return None


# ----------------------------------------------------------------------------- screen state

def screen_locked() -> bool:
    """Whether the session reports itself locked.

    A locked Mac still captures, but drops synthetic input, so a viewer would see a live picture that
    ignores every tap. Note that a credential panel sets the same bit (see ``auth_prompt``)."""
    try:
        d = _quartz().CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked"))
    except Exception:
        return False


def blocked_by() -> str | None:
    """The name of a system dialog currently holding focus, if one is.

    All windows on all layers are checked, because these dialogs do not live on layer 0."""
    try:
        return find_blocker(_raw_windows())
    except Exception:
        return None


def auth_prompt() -> dict | None:
    """The credential panel (SecurityAgent) on screen, as ``{wid, owner, title}``, or None.

    When macOS asks for a password to allow a change, it shows a separate floating panel owned by
    SecurityAgent, not by the application that asked. The panel runs in secure input mode, so it
    ignores synthetic keys and clicks (``claude_human.unlock.approve`` types into it through the
    virtual HID keyboard). While it is up, ``CGSSessionScreenIsLocked`` reads True even though the
    desktop behind it is live and can be captured. The window id is looked up on every call."""
    try:
        q = _quartz()
        disp = q.CGDisplayBounds(q.CGMainDisplayID())
        raw = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []
        return find_auth_panel(raw, (disp.size.width, disp.size.height))
    except Exception:
        return None


# ----------------------------------------------------------------------------- capture

def _sckshot_frame(wid: int | None, max_w: int, binary: str | os.PathLike) -> tuple[bytes, int, int] | None:
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        r = subprocess.run(sckshot_args(binary, tmp, wid, max_w), capture_output=True, timeout=6)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            return None
        with open(tmp, "rb") as fh:
            data = fh.read()
        size = parse_sckshot_output(r.stdout)
        w, h = size if size else (int(max_w), 0)
        return data, w, h
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _grab_legacy(wid: int | None):
    """A CGImage through CGWindowListCreateImage, or None.

    The per-window variant returns None on recent macOS, so a window is the whole display cropped to
    the window's rectangle. When one window is asked for and the rectangle cannot be found (most
    often because the window is on another desktop), this returns None and never the full screen:
    returning the full screen would show every other window to someone who was given only one."""
    q = _quartz()
    try:
        full = q.CGWindowListCreateImage(q.CGRectInfinite, q.kCGWindowListOptionOnScreenOnly,
                                         q.kCGNullWindowID, q.kCGWindowImageNominalResolution)
        if not wid:
            return full
        r = window_rect(wid)
        if full is None or r is None:
            return None
        return q.CGImageCreateWithImageInRect(
            full, q.CGRectMake(r.origin.x, r.origin.y, r.size.width, r.size.height))
    except Exception:
        return None


def _downscale(img, max_w: int):
    q = _quartz()
    w, h = q.CGImageGetWidth(img), q.CGImageGetHeight(img)
    if w <= max_w:
        return img, w, h
    scale = max_w / float(w)
    nw, nh = int(w * scale), int(h * scale)
    ctx = q.CGBitmapContextCreate(None, nw, nh, 8, 0, q.CGColorSpaceCreateDeviceRGB(),
                                  q.kCGImageAlphaPremultipliedFirst)
    q.CGContextSetInterpolationQuality(ctx, q.kCGInterpolationMedium)
    q.CGContextDrawImage(ctx, q.CGRectMake(0, 0, nw, nh), img)
    return q.CGBitmapContextCreateImage(ctx), nw, nh


def _jpeg(img, quality: float) -> bytes:
    q = _quartz()
    from CoreFoundation import CFDataCreateMutable  # noqa: PLC0415
    data = CFDataCreateMutable(None, 0)
    dest = q.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    q.CGImageDestinationAddImage(dest, img, {"kCGImageDestinationLossyCompressionQuality": quality})
    q.CGImageDestinationFinalize(dest)
    return bytes(data)


def capture(wid: int | None = None, *, max_width: int = DEFAULT_MAX_WIDTH,
            quality: float = DEFAULT_QUALITY,
            sckshot: str | os.PathLike | None = None) -> tuple[bytes, int, int] | None:
    """One JPEG of the screen (``wid`` None) or of one window: ``(bytes, width, height)`` or None.

    This does not check the lock state. Use ``frame`` for that."""
    fr = _sckshot_frame(wid, max_width, paths.sckshot_path(sckshot))
    if fr is not None:
        return fr
    img = _grab_legacy(wid)
    if img is None:
        return None
    img, w, h = _downscale(img, max_width)
    return _jpeg(img, quality), w, h


def frame(wid: int | None = None, **kw: Any) -> tuple[bytes, int, int] | None:
    """Like ``capture``, but None at once while the session is locked.

    While locked, macOS refuses the capture and the fallback path blocks for about 30 seconds before
    returning black. A credential panel is the exception: it sets the locked bit while the desktop
    is live, so a frame is still taken when ``auth_prompt()`` finds one."""
    if screen_locked() and auth_prompt() is None:
        return None
    return capture(wid, **kw)


def save(out: str | os.PathLike, wid: int | None = None, *, max_width: int = 0,
         sckshot: str | os.PathLike | None = None) -> tuple[int, int]:
    """Write one capture to ``out`` with sckshot (PNG, or JPEG for .jpg/.jpeg). Returns its size.

    Raises FileNotFoundError when sckshot is not built and RuntimeError when the capture fails."""
    binary = paths.sckshot_path(sckshot)
    if not binary.exists():
        raise FileNotFoundError(f"sckshot is not built at {binary} (run: claude-human build-tools)")
    r = subprocess.run(sckshot_args(binary, str(out), wid, max_width), capture_output=True, timeout=20)
    size = parse_sckshot_output(r.stdout)
    if r.returncode != 0 or size is None:
        raise RuntimeError((r.stderr or r.stdout or b"").decode(errors="replace").strip() or "capture failed")
    return size
