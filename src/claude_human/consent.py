"""Find a macOS consent prompt on screen, read it, and press one of its buttons.

When a program first sends Apple Events to another application, or asks for a protected resource,
macOS shows a small prompt such as "Terminal wants access to control Finder" with two buttons,
"Don't Allow" and "Allow". The prompt is owned by UserNotificationCenter, not by the program that
asked, and it waits until a person answers. Until then every Apple Event from that program to that
application hangs, and so does every later request queued behind it. A prompt raised by a job
nobody is watching can therefore stop a machine's automation for hours without any error.

Synthetic clicks are ignored on these prompts, so a button is pressed through Karabiner's virtual
HID keyboard (the same ``vhid_type`` helper ``claude_human.unlock`` uses). On the prompt "Allow" is
the default button, which Return presses, and the keyboard focus ring sits on "Don't Allow", which
Space presses (Full Keyboard Access must be on, as for ``unlock.approve``).

Rules every function here keeps:

* Nothing is pressed unless a prompt is on screen and its owner is the frontmost application.
* The result is observed: a press counts only when the prompt window is gone afterwards.
* The answer comes from a person. This module presses what it is told and decides nothing.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable, Mapping
from typing import Callable, Optional

from . import paths

#: who shows consent prompts
CONSENT_OWNERS = {"UserNotificationCenter"}
#: a prompt is a small floating panel: larger than a menu bar item, smaller than this share of the
#: display (a full-screen surface of the same owner is not a prompt)
MIN_SIDE = 120
MAX_FRACTION = 0.6
#: the answers, and the key each one is pressed with
KEYS = {"allow": ("", ("--return",)), "deny": (" ", ())}

#: Hooks for the outside world, replaced by tests.
_run = subprocess.run
_sleep = time.sleep
_now = time.time
_exists = os.path.exists

Result = tuple[bool, str]


def find_prompts(raw: Iterable[dict], display: tuple[float, float],
                 owners: set[str] = CONSENT_OWNERS) -> list[dict]:
    """Pure. The consent prompts among on-screen windows (front to back), as
    ``{wid, pid, owner, x, y, w, h}``."""
    cap_w, cap_h = display[0] * MAX_FRACTION, display[1] * MAX_FRACTION
    out = []
    for w in raw:
        if str(w.get("kCGWindowOwnerName") or "") not in owners:
            continue
        if float(w.get("kCGWindowAlpha", 1)) <= 0:
            continue
        b = w.get("kCGWindowBounds", {}) or {}
        ww, hh = float(b.get("Width", 0)), float(b.get("Height", 0))
        if ww < MIN_SIDE or hh < MIN_SIDE or ww > cap_w or hh > cap_h:
            continue
        out.append({"wid": int(w.get("kCGWindowNumber") or 0), "pid": int(w.get("kCGWindowOwnerPID") or 0),
                    "owner": str(w.get("kCGWindowOwnerName") or ""), "x": float(b.get("X", 0)),
                    "y": float(b.get("Y", 0)), "w": ww, "h": hh})
    return out


def prompts() -> list[dict] | None:
    """The consent prompts on screen now, or None when the window list cannot be read."""
    try:
        from .screenshot import _quartz
        q = _quartz()
        disp = q.CGDisplayBounds(q.CGMainDisplayID())
        raw = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []
        return find_prompts(raw, (disp.size.width, disp.size.height))
    except Exception:
        return None


def join_lines(lines: Iterable[str]) -> str:
    """Pure. OCR lines of a prompt as one sentence: the button labels and the help mark dropped."""
    skip = {"allow", "don't allow", "don’t allow", "ok", "?", "deny"}
    kept = [s.strip() for s in lines if s.strip() and s.strip().lower() not in skip]
    return " ".join(kept).replace("“", '"').replace("”", '"').replace("’", "'")


def read_text(image_path: str | os.PathLike) -> str | None:
    """The prompt's words, read from a capture with the Vision framework, or None."""
    try:
        import Vision
        from Foundation import NSURL
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(0)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(str(image_path)), None)
        ok, _ = handler.performRequests_error_([req], None)
        if not ok:
            return None
        lines = [r.topCandidates_(1)[0].string() for r in (req.results() or [])]
        return join_lines(lines) or None
    except Exception:
        return None


def capture(prompt: dict, out: str | os.PathLike, *, env: Mapping[str, str] | None = None) -> bool:
    """Save a picture of the prompt's rectangle to ``out`` (PNG). False when it cannot be taken,
    which is the case while the screen is locked."""
    rect = f"{int(prompt['x'])},{int(prompt['y'])},{int(prompt['w'])},{int(prompt['h'])}"
    try:
        r = _run(["/usr/sbin/screencapture", "-x", "-R", rect, str(out)], capture_output=True,
                 timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and _exists(str(out))


def bring_forward(pid: int) -> bool:
    """Make the prompt's owner the frontmost application, so the keyboard reaches the prompt."""
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSWorkspace
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is None:
            return False
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        for _ in range(10):
            _sleep(0.15)
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and int(front.processIdentifier()) == int(pid):
                return True
        return False
    except Exception:
        return False


def answer(choice: str, *, helper: str | os.PathLike | None = None, poll_seconds: float = 5.0,
           find: Callable[[], Optional[list]] | None = None,
           forward: Callable[[int], bool] | None = None, sudo: bool = True,
           env: Mapping[str, str] | None = None) -> Result:
    """Press "Allow" (``choice="allow"``) or "Don't Allow" (``"deny"``) on the frontmost consent
    prompt, and observe it close. ``find`` lists the prompts (default ``prompts``), ``forward``
    brings a pid's application to the front (default ``bring_forward``)."""
    if choice not in KEYS:
        return False, f"unknown answer {choice!r}: allow or deny"
    find = find or prompts
    forward = forward or bring_forward
    now = find()
    if now is None:
        return False, "the window list cannot be read (is the screen locked?)"
    if not now:
        return False, "No consent prompt is open on the Mac right now."
    target = now[0]
    if not forward(target["pid"]):
        return False, "could not bring the prompt to the front, so nothing was pressed"
    helper = str(paths.vhid_path(helper))
    if not _exists(helper):
        return False, f"virtual HID helper not built ({helper})"
    text, flags = KEYS[choice]
    argv = (["sudo", "-n"] if sudo else []) + [helper, *flags]
    try:
        r = _run(argv, input=text, text=True, capture_output=True, timeout=20, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"could not run the virtual HID helper: {str(e)[:60]}"
    if r.returncode != 0:
        return False, f"virtual HID helper failed: {(r.stderr or r.stdout or '').strip()[:80]}"
    deadline = _now() + poll_seconds
    while _now() < deadline:
        _sleep(0.3)
        left = find()
        if left is not None and all(p["wid"] != target["wid"] for p in left):
            return True, "Allowed." if choice == "allow" else "Not allowed."
    return False, "pressed the key, but the prompt is still open"
