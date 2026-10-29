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

Gatekeeper asks too, the first time an app downloaded from the Internet opens: '"X" is an app
downloaded from the Internet. Are you sure you want to open it?', with "Open" and "Cancel". That
prompt is owned by CoreServicesUIAgent, and the app it is about waits stopped, with no window, until
someone answers (so does an ``open -a`` that started it). Its buttons are pressed by their own titles
through the Accessibility API (``press_button``), which needs a process with the Accessibility grant,
such as the station (``claude_human.station``'s ``consent`` input). Each owner's buttons are in
``BUTTONS``.

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

#: who shows consent prompts, and the titles of the buttons that allow and that deny
BUTTONS = {"UserNotificationCenter": ("Allow", "Don't Allow"),
           "CoreServicesUIAgent": ("Open", "Cancel")}
CONSENT_OWNERS = set(BUTTONS)
#: the owners whose prompts are pressed by a button's title through the Accessibility API
AX_OWNERS = {"CoreServicesUIAgent"}
#: how deep under a prompt window its buttons are looked for
AX_DEPTH = 8
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
    ``{wid, pid, owner, x, y, w, h, allow, deny}``; ``allow`` and ``deny`` are the titles of the
    owner's two buttons (``BUTTONS``)."""
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
        owner = str(w.get("kCGWindowOwnerName") or "")
        allow, deny = BUTTONS.get(owner, ("Allow", "Don't Allow"))
        out.append({"wid": int(w.get("kCGWindowNumber") or 0), "pid": int(w.get("kCGWindowOwnerPID") or 0),
                    "owner": owner, "x": float(b.get("X", 0)),
                    "y": float(b.get("Y", 0)), "w": ww, "h": hh, "allow": allow, "deny": deny})
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
    skip = {"allow", "don't allow", "don’t allow", "ok", "?", "deny", "open", "cancel", "show web page"}
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
    which is the case while the screen is locked (``capture_why`` says why)."""
    return capture_why(prompt, out, env=env) is None


def capture_why(prompt: dict, out: str | os.PathLike, *, env: Mapping[str, str] | None = None) -> str | None:
    """Save a picture of the prompt's rectangle to ``out`` (PNG): None when the file is there, else
    why not. screencapture can exit 0 and write nothing when the process may not record the screen,
    so the file itself is the test."""
    rect = f"{int(prompt['x'])},{int(prompt['y'])},{int(prompt['w'])},{int(prompt['h'])}"
    try:
        r = _run(["/usr/sbin/screencapture", "-x", "-R", rect, str(out)], capture_output=True,
                 timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"screencapture did not run: {e}"
    said = (getattr(r, "stderr", b"") or b"")
    said = (said.decode(errors="replace") if isinstance(said, bytes) else str(said)).strip()[:200]
    if r.returncode != 0:
        return f"screencapture exited {r.returncode}" + (f": {said}" if said else "")
    if not _exists(str(out)):
        return ("screencapture wrote no file (this process may not be allowed to record the screen)"
                + (f": {said}" if said else ""))
    return None


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


def _title(text) -> str:
    return str(text or "").replace("’", "'").strip()


def _window_id(element, ax) -> int | None:
    """The window server's id of an accessibility window, through the private call every window
    manager uses (``_AXUIElementGetWindow``), or None when it cannot be asked."""
    get = getattr(ax, "window_id", None)
    if get is None:
        try:
            import objc   # noqa: PLC0415
            from Foundation import NSBundle   # noqa: PLC0415
            found: dict = {}
            objc.loadBundleFunctions(
                NSBundle.bundleWithPath_("/System/Library/Frameworks/ApplicationServices.framework"),
                found, [("_AXUIElementGetWindow", b"i^{__AXUIElement=}o^I")])
            get = found.get("_AXUIElementGetWindow")
        except Exception:  # noqa: BLE001
            return None
        if get is None:
            return None
        err, wid = get(element, None)
        return int(wid) if err == 0 else None
    return get(element)


def press_button(pid: int, wid: int, title: str, *, ax=None) -> Result:
    """Press the button titled ``title`` in the window ``wid`` of the process ``pid``, through the
    Accessibility API (``AXPress``). Nothing is pressed when this process has no Accessibility
    grant, the window is not among the process's accessibility windows, or no button has that
    title. ``ax`` is the ApplicationServices module or a stand-in."""
    if ax is None:
        try:
            import ApplicationServices as ax   # noqa: PLC0415, N813
        except ImportError:
            return False, "the Accessibility API is not available here"
    if not ax.AXIsProcessTrusted():
        return False, "this process has no Accessibility grant (the station has one)"

    def attr(element, name):
        err, value = ax.AXUIElementCopyAttributeValue(element, name, None)
        return value if err == 0 else None

    app = ax.AXUIElementCreateApplication(int(pid))
    windows = [w for w in (attr(app, "AXWindows") or []) if attr(w, "AXRole") == "AXWindow"]
    mine = [w for w in windows if _window_id(w, ax) == int(wid)]
    if not mine:
        return False, f"window {wid} is not among the accessibility windows of its process"
    want, level = _title(title), [mine[0]]
    for _ in range(AX_DEPTH):
        for element in level:
            if attr(element, "AXRole") == "AXButton" and _title(attr(element, "AXTitle")) == want:
                err = ax.AXUIElementPerformAction(element, "AXPress")
                return (True, f"pressed {title}") if err == 0 else (False, f"{title} did not take the press ({err})")
        level = [child for element in level for child in (attr(element, "AXChildren") or [])]
    return False, f"the prompt has no button titled {title!r}"


def _closed(find: Callable[[], Optional[list]], target: dict, poll_seconds: float, said: str) -> Result:
    deadline = _now() + poll_seconds
    while _now() < deadline:
        _sleep(0.3)
        left = find()
        if left is not None and all(p["wid"] != target["wid"] for p in left):
            return True, said
    return False, "pressed the key, but the prompt is still open"


def answer(choice: str, *, helper: str | os.PathLike | None = None, poll_seconds: float = 5.0,
           find: Callable[[], Optional[list]] | None = None,
           forward: Callable[[int], bool] | None = None, sudo: bool = True,
           env: Mapping[str, str] | None = None,
           press: Callable[[int, int, str], Result] | None = None) -> Result:
    """Press "Allow" (``choice="allow"``) or "Don't Allow" (``"deny"``) on the frontmost consent
    prompt, and observe it close. ``find`` lists the prompts (default ``prompts``), ``forward``
    brings a pid's application to the front (default ``bring_forward``).

    A prompt of an owner in ``AX_OWNERS`` (Gatekeeper's "Open" and "Cancel") is pressed by its
    button's title with ``press`` (default ``press_button``) instead of the keyboard, and needs no
    helper and no bringing forward."""
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
    owner = target.get("owner") or "UserNotificationCenter"
    if owner in AX_OWNERS:
        title = BUTTONS[owner][0 if choice == "allow" else 1]
        ok, said = (press or press_button)(target["pid"], target["wid"], title)
        if not ok:
            return False, f"nothing was pressed: {said}"
        ok, said = _closed(find, target, poll_seconds, f"Pressed {title}.")
        return (ok, said) if ok else (False, f"pressed {title}, but the prompt is still open")
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
    return _closed(find, target, poll_seconds, "Allowed." if choice == "allow" else "Not allowed.")


def ask_station(choice: str, *, token: str, station: str = "http://127.0.0.1:8789",
                opener: Callable | None = None) -> Result:
    """Answer the front consent prompt through a running station, which presses it with its own
    Accessibility grant (``{"type": "consent"}`` on ``/input``, master token only). For a process
    that has no such grant, such as a launchd job. The station's answer, or why it could not be
    asked."""
    import json   # noqa: PLC0415
    import urllib.error   # noqa: PLC0415
    import urllib.request   # noqa: PLC0415
    if choice not in ("allow", "deny"):
        return False, f"unknown answer {choice!r}: allow or deny"
    req = urllib.request.Request(
        f"{station.rstrip('/')}/input", method="POST",
        data=json.dumps({"type": "consent", "choice": choice}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:
            got = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return False, f"the station answered {e.code}"
    except (OSError, ValueError) as e:
        return False, f"the station did not answer: {type(e).__name__}"
    if not isinstance(got, dict):
        return False, "the station's answer was not an object"
    return bool(got.get("ok")), str(got.get("detail") or ("pressed" if got.get("ok") else "refused"))
