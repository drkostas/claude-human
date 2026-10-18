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
import plistlib
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import quote
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
    import Quartz
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


#: system chrome that lies in front of every window and draws no other application's content: the
#: Dock keeps an opaque window the size of the whole display (measured: layer 20, alpha 1.0), the
#: Window Server the cursor and the menu bar's backdrop, Control Center and SystemUIServer the menu
#: bar's items. Counting them would refuse every window. Notification Center is NOT here: its banners
#: carry other applications' messages.
CHROME = frozenset({"Dock", "Window Server", "Control Center", "SystemUIServer"})
#: below this a window shows nothing a viewer could read (an overlay measured at 0.003)
INVISIBLE = 0.05


def covered_by(raw: Iterable[dict], wid: int) -> str | None:
    """The owner of the first window that lies in front of window ``wid`` and overlaps it, or None.

    ``raw`` is the on-screen list from CGWindowListCopyWindowInfo, which runs front to back, so
    every entry before ``wid`` is in front of it. A window of the same application (a sheet, a
    popover) is part of what a grant for that application shows, a window with almost no opacity
    shows nothing, and system chrome (``CHROME``) draws no other application's content, so none of
    them counts. ``wid`` missing from the list reads as covered (an empty
    owner), because a window that is not on screen cannot be shown either.

    The capture without sckshot crops the whole display to a window's rectangle, so whatever lies
    on top of the window would come through instead of it: another application's page, given to
    someone who was granted only that one window."""
    ahead: list[dict] = []
    target = None
    for w in raw:
        if w.get("kCGWindowNumber") == wid:
            target = w
            break
        ahead.append(w)
    if target is None:
        return ""
    tb = target.get("kCGWindowBounds") or {}
    tx, ty = float(tb.get("X", 0)), float(tb.get("Y", 0))
    tw, th = float(tb.get("Width", 0)), float(tb.get("Height", 0))
    mine = target.get("kCGWindowOwnerPID"), str(target.get("kCGWindowOwnerName") or "")
    for w in ahead:
        if float(w.get("kCGWindowAlpha", 1.0) or 0.0) <= INVISIBLE:
            continue
        pid, owner = w.get("kCGWindowOwnerPID"), str(w.get("kCGWindowOwnerName") or "")
        if owner in CHROME:
            continue
        if (pid is not None and pid == mine[0]) or (pid is None and owner and owner == mine[1]):
            continue
        b = w.get("kCGWindowBounds") or {}
        x, y = float(b.get("X", 0)), float(b.get("Y", 0))
        ww, wh = float(b.get("Width", 0)), float(b.get("Height", 0))
        if min(tx + tw, x + ww) > max(tx, x) and min(ty + th, y + wh) > max(ty, y):
            return owner or "an unnamed window"
    return None


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


def window_ids() -> set[int] | None:
    """The id of every window the window server has, on EVERY desktop, off-screen and minimised
    ones included. yabai's window ids are these ids, so a window whose id is missing here is
    closed. None when it could not be read: an empty answer from a running Mac is a failed read,
    never "no windows"."""
    try:
        q = _quartz()
        listed = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll, q.kCGNullWindowID)
        ids = {int(w["kCGWindowNumber"]) for w in (listed or [])}
    except Exception:  # noqa: BLE001 - not able to ask is unknown
        return None
    return ids or None


#: apps never brought back with ``open -b``. To an app that is already running, ``open -b`` is a
#: reopen, and Finder answers a reopen with no window open by opening a new window at its default
#: folder, on whatever desktop is showing. Finder is in front when the desktop itself has focus, so
#: there is no window of it to give focus back to, and asking only made a window appear.
NO_REOPEN = frozenset({"com.apple.finder"})


def activate(bundle_id: str | None, *, env: Mapping[str, str] | None = None) -> bool:
    """Bring an application to the front by its bundle id, with ``open -b``. False when it was not
    asked: no bundle id, or an app in NO_REOPEN.

    LaunchServices does it, so no Automation grant is needed. ⚠️ AppKit's activateWithOptions is not
    reliable for this: measured 2026-10-11, it brought iTerm back right after another app made a
    window, but was ignored right after Brave had been activated. ``open -b`` worked in both."""
    if not bundle_id or bundle_id in NO_REOPEN:
        return False
    try:
        r = subprocess.run(["/usr/bin/open", "-b", bundle_id], capture_output=True, text=True,
                           timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


#: the bundle id in front while the screen is locked
LOGIN_WINDOW = "com.apple.loginwindow"


def keep_front(was: str | None, front_app, *, watch: float = 1.5, step: float = 0.25,
               env: Mapping[str, str] | None = None) -> str | None:
    """Give the person back the app they were in (`was`, a bundle id) when another app takes the
    front within `watch` seconds; returns what was given back, or None. `front_app()` answers the
    bundle id in front now (claude_ops.update.front_app reads it without an Automation grant).

    For any automation that opens a window in the person's own browser while they work: making a
    browser window, or setting a tab's URL by AppleScript, brings the browser forward, and
    ``open -g`` does not stop it. Nothing is done on a locked screen or when `was` is unknown."""
    import time   # noqa: PLC0415
    if not was or was == LOGIN_WINDOW:
        return None
    end = time.time() + watch
    while True:
        now = front_app()
        if now and now not in (was, LOGIN_WINDOW):
            activate(was, env=env)
            return was
        if time.time() >= end:
            return None
        time.sleep(step)


def front_window(raw: Iterable[dict] | None = None) -> int | None:
    """The id of the frontmost ordinary window, the one typed text and keys reach.

    CGWindowListCopyWindowInfo lists on-screen windows front to back, so the first entry that is an
    ordinary window (layer 0, the minimum size) is in front. None when there is none."""
    for w in (raw if raw is not None else _raw_windows()):
        if to_windows([w]):
            return int(w.get("kCGWindowNumber"))
    return None


def resolve(app: str | None, wid: int | None) -> int | None:
    """Which window a request means right now.

    A CGWindowID changes every time an application restarts, so storing one and using it later can
    reach a different window. Pass the application name and the id is looked up again on every
    call. With no application name, ``wid`` is returned as it is.

    With both, the answer is that window and no other, and only while it is still on screen and
    still the application's. Two windows of one app are the case an app name cannot tell apart, so
    a window that has gone is None, never the app's other window."""
    if app and wid:
        want = app.lower()
        ok = any(w["id"] == wid and (w["app"].lower() == want or want in w["app"].lower())
                 for w in windows())
        return wid if ok else None
    if app:
        return pick_window(windows(), app)
    return wid


def window_rect(wid: int):
    for w in windows():
        if w["id"] == wid:
            return _quartz().CGRectMake(w["x"], w["y"], w["width"], w["height"])
    return None


# ----------------------------------------------------------------------------- screen state

def screen_locked() -> bool | None:
    """Whether the session reports itself locked. ``None`` when that cannot be read.

    A locked Mac still captures, but drops synthetic input, so a viewer would see a live picture that
    ignores every tap. Note that a credential panel sets the same bit (see ``auth_prompt``).

    A process outside the login session (a launchd daemon, anything started in the system domain)
    gets no session dictionary at all. That is not an unlocked screen: reading it as one told such a
    process "unlocked" while the Mac sat locked. So no dictionary, or no Quartz, is ``None``, and a
    caller that only tests truthiness behaves as before."""
    try:
        d = _quartz().CGSessionCopyCurrentDictionary()
    except Exception:
        return None
    if d is None:
        return None
    return bool(d.get("CGSSessionScreenIsLocked"))


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


# ----------------------------------------------------------------------------- capture approvals

#: Where macOS 15 and later keep, for each application that records the screen without the system
#: picker, when they last asked the person to confirm it ("... is requesting to bypass the system
#: private window picker", answered with "Allow For One Month") and when they will ask again. The
#: file belongs to the system and is only read here.
CAPTURE_APPROVALS = "~/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist"


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    # plistlib returns naive datetimes in UTC (an aware option exists only from Python 3.13)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_capture_approvals(data: Mapping[str, Any] | None) -> dict[str, dict]:
    """Pure. Each application's entry as ``{"asked", "asks_again", "every_s"}``: when macOS last
    asked to confirm its screen recording, when it will ask again (UTC datetimes, or None) and the
    interval in seconds. The key is the one macOS uses: a bundle id, an executable path, or the
    file URL of an app bundle (``capture_approval_of`` finds an app under any of them)."""
    out: dict[str, dict] = {}
    for app, entry in (data or {}).items():
        if not isinstance(entry, Mapping):
            continue
        every = entry.get("kScreenCapturePrivacyHintPolicy")
        out[str(app)] = {"asked": _utc(entry.get("kScreenCaptureApprovalLastAlerted")),
                         "asks_again": _utc(entry.get("kScreenCapturePrivacyHintDate")),
                         "every_s": int(every) if isinstance(every, (int, float)) else None}
    return out


def capture_approvals(path: str | os.PathLike | None = None) -> dict[str, dict] | None:
    """Every application's screen-recording confirmation (``parse_capture_approvals``), or None
    when the file cannot be read: an older macOS, or a process without access to it."""
    try:
        with open(os.path.expanduser(str(path or CAPTURE_APPROVALS)), "rb") as fh:
            return parse_capture_approvals(plistlib.load(fh))
    except Exception:
        return None


def capture_approval_of(approvals: Mapping[str, dict] | None, app: str) -> dict | None:
    """Pure. The entry for one application, given as a bundle id or as the path of its ``.app``
    bundle, which macOS keys as a file URL with a trailing slash. None when it has none."""
    if not approvals or not app:
        return None
    keys = [app]
    if app.startswith("/"):
        bundle = app.rstrip("/")
        keys += [bundle, "file://" + quote(bundle) + "/"]
    return next((approvals[k] for k in keys if k in approvals), None)


# ----------------------------------------------------------------------------- capture

def _sckshot_frame(wid: int | None, max_w: int, binary: str | os.PathLike, *, env: Mapping[str, str] | None = None) -> tuple[bytes, int, int] | None:
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        r = subprocess.run(sckshot_args(binary, tmp, wid, max_w), capture_output=True, timeout=6, env=env)
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
    returning the full screen would show every other window to someone who was given only one.

    ⛔ AND A WINDOW ANOTHER APPLICATION COVERS IS REFUSED TOO (reported by Dennis, 0.5.0). The crop
    is of the display, not of the window, so with Arc on top of System Settings a capture of
    Settings returned Arc's page. Whatever lies in front of the window is checked before and after
    the grab (``covered_by``), and a covered window gives None, as a missing one does. The sckshot
    path captures the window itself and does not need this."""
    q = _quartz()
    try:
        if wid and covered_by(_raw_windows(), wid) is not None:
            return None
        full = q.CGWindowListCreateImage(q.CGRectInfinite, q.kCGWindowListOptionOnScreenOnly,
                                         q.kCGNullWindowID, q.kCGWindowImageNominalResolution)
        if not wid:
            return full
        r = window_rect(wid)
        if full is None or r is None:
            return None
        if covered_by(_raw_windows(), wid) is not None:
            return None                          # something moved on top while the grab ran
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
    from CoreFoundation import CFDataCreateMutable
    data = CFDataCreateMutable(None, 0)
    dest = q.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    q.CGImageDestinationAddImage(dest, img, {"kCGImageDestinationLossyCompressionQuality": quality})
    q.CGImageDestinationFinalize(dest)
    return bytes(data)


def capture(wid: int | None = None, *, max_width: int = DEFAULT_MAX_WIDTH,
            quality: float = DEFAULT_QUALITY,
            sckshot: str | os.PathLike | None = None, env: Mapping[str, str] | None = None) -> tuple[bytes, int, int] | None:
    """One JPEG of the screen (``wid`` None) or of one window: ``(bytes, width, height)`` or None.

    This does not check the lock state. Use ``frame`` for that."""
    fr = _sckshot_frame(wid, max_width, paths.sckshot_path(sckshot), env=env)
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
         sckshot: str | os.PathLike | None = None, env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """Write one capture to ``out`` with sckshot (PNG, or JPEG for .jpg/.jpeg). Returns its size.

    Raises FileNotFoundError when sckshot is not built and RuntimeError when the capture fails."""
    binary = paths.sckshot_path(sckshot)
    if not binary.exists():
        raise FileNotFoundError(f"sckshot is not built at {binary} (run: claude-human build-tools)")
    r = subprocess.run(sckshot_args(binary, str(out), wid, max_width), capture_output=True, timeout=20, env=env)
    size = parse_sckshot_output(r.stdout)
    if r.returncode != 0 or size is None:
        raise RuntimeError((r.stderr or r.stdout or b"").decode(errors="replace").strip() or "capture failed")
    return size
