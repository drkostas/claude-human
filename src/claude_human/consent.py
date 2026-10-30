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
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from typing import Callable, Optional

from . import paths

#: who shows consent prompts, and the titles of the buttons that allow and that deny
BUTTONS = {"UserNotificationCenter": ("Allow", "Don't Allow"),
           "CoreServicesUIAgent": ("Open", "Cancel"),
           "universalAccessAuthWarn": ("Open System Settings", "Deny")}
CONSENT_OWNERS = set(BUTTONS)
#: the owners whose prompt only the person can grant, in System Settings (an app asking to control
#: the computer through accessibility features): such a prompt is listed with ``kind`` "settings",
#: and ``answer`` refuses to allow it, since its first button only opens System Settings
SETTINGS_OWNERS = {"universalAccessAuthWarn"}
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


def _may_record() -> bool | None:
    """Whether this process may record the screen, asked without showing a prompt
    (CGPreflightScreenCaptureAccess). None when it cannot be told."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return None

Result = tuple[bool, str]


def find_prompts(raw: Iterable[dict], display: tuple[float, float],
                 owners: set[str] = CONSENT_OWNERS) -> list[dict]:
    """Pure. The consent prompts among the windows (front to back), as
    ``{wid, pid, owner, x, y, w, h, allow, deny, kind, on_screen}``; ``allow`` and ``deny`` are the
    titles of the owner's two buttons (``BUTTONS``), ``kind`` is "settings" for a prompt only the
    person can grant in System Settings (``SETTINGS_OWNERS``) and "press" otherwise, and
    ``on_screen`` is False for one on another desktop."""
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
                    "y": float(b.get("Y", 0)), "w": ww, "h": hh, "allow": allow, "deny": deny,
                    "kind": "settings" if owner in SETTINGS_OWNERS else "press",
                    "on_screen": bool(w.get("kCGWindowIsOnscreen", True))})
    return out


def prompts() -> list[dict] | None:
    """The consent prompts open now, on screen first (front to back), then those on other desktops,
    or None when the window list cannot be read. A prompt on a desktop nobody is looking at waits as
    long as one in front of them."""
    try:
        from .screenshot import _quartz
        q = _quartz()
        disp = q.CGDisplayBounds(q.CGMainDisplayID())
        shown = list(q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or [])
        seen = {w.get("kCGWindowNumber") for w in shown}
        rest = [w for w in (q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll, q.kCGNullWindowID) or [])
                if w.get("kCGWindowNumber") not in seen and str(w.get("kCGWindowOwnerName") or "") in CONSENT_OWNERS]
        return find_prompts(shown + rest, (disp.size.width, disp.size.height))
    except Exception:
        return None


def join_lines(lines: Iterable[str]) -> str:
    """Pure. OCR lines of a prompt as one sentence: the button labels and the help mark dropped."""
    skip = {"allow", "don't allow", "don’t allow", "ok", "?", "deny", "open", "cancel", "show web page",
            "open system settings"}
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
    so the file itself is the test. A process that may not record the screen does not run it at all:
    macOS answers each such screencapture with a new screen recording prompt naming this process,
    so a caller that tries once a minute would put one in front of the person every minute."""
    if _may_record() is False:
        return ("this process may not record the screen, so no picture was taken (screencapture would "
                "only make macOS ask the person again)")
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


def _ax_window(pid: int, wid: int, ax):
    """(the accessibility window `wid` of process `pid`, an attribute reader) or (None, why not)."""
    if ax is None:
        try:
            import ApplicationServices as ax   # noqa: PLC0415, N813
        except ImportError:
            return None, "the Accessibility API is not available here", None
    if not ax.AXIsProcessTrusted():
        return None, "this process has no Accessibility grant (the station has one)", ax

    def attr(element, name):
        err, value = ax.AXUIElementCopyAttributeValue(element, name, None)
        return value if err == 0 else None

    app = ax.AXUIElementCreateApplication(int(pid))
    windows = [w for w in (attr(app, "AXWindows") or []) if attr(w, "AXRole") == "AXWindow"]
    mine = [w for w in windows if _window_id(w, ax) == int(wid)]
    if not mine:
        return None, f"window {wid} is not among the accessibility windows of its process", ax
    return mine[0], attr, ax


def _elements(window, attr):
    """Every element under `window`, breadth first, AX_DEPTH levels deep."""
    level = [window]
    for _ in range(AX_DEPTH):
        yield from level
        level = [child for element in level for child in (attr(element, "AXChildren") or [])]


def read_words(pid: int, wid: int, *, ax=None) -> str | None:
    """The prompt's words, read from its text through the Accessibility API, with the quotes
    straightened, or None when they cannot be read (no Accessibility grant, the window gone). It
    reads a locked or covered screen as well as an open one, where a picture cannot."""
    window, attr, ax = _ax_window(pid, wid, ax)
    if window is None:
        return None
    parts = [str(attr(e, "AXValue") or "").strip() for e in _elements(window, attr)
             if attr(e, "AXRole") == "AXStaticText"]
    words = join_lines(p for p in parts if p)
    return words or None


def press_button(pid: int, wid: int, title: str, *, ax=None) -> Result:
    """Press the button titled ``title`` in the window ``wid`` of the process ``pid``, through the
    Accessibility API (``AXPress``). Nothing is pressed when this process has no Accessibility
    grant, the window is not among the process's accessibility windows, or no button has that
    title. ``ax`` is the ApplicationServices module or a stand-in."""
    window, attr, ax = _ax_window(pid, wid, ax)
    if window is None:
        return False, attr
    want, level = _title(title), [window]
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
           press: Callable[[int, int, str], Result] | None = None, by_title: bool = False,
           tries: int = 2, wid: int | None = None) -> Result:
    """Press "Allow" (``choice="allow"``) or "Don't Allow" (``"deny"``) on the frontmost consent
    prompt, and observe it close. ``find`` lists the prompts (default ``prompts``), ``forward``
    brings a pid's application to the front (default ``bring_forward``).

    A prompt of an owner in ``AX_OWNERS`` (Gatekeeper's "Open" and "Cancel") is pressed by its
    button's title with ``press`` (default ``press_button``) instead of the keyboard, and needs no
    helper and no bringing forward. With ``by_title`` every prompt is pressed that way, which a
    process with the Accessibility grant (the station) can do for any owner. A press by title that
    leaves the prompt open is tried again, ``tries`` times in all."""
    if choice not in KEYS:
        return False, f"unknown answer {choice!r}: allow or deny"
    find = find or prompts
    forward = forward or bring_forward
    now = find()
    if now is None:
        return False, "the window list cannot be read (is the screen locked?)"
    if not now:
        return False, "No consent prompt is open on the Mac right now."
    if wid is not None:
        # the prompt asked about, never whichever one happens to be in front
        named = [p for p in now if int(p.get("wid", 0)) == int(wid)]
        if not named:
            return False, f"prompt window {wid} is no longer open, so nothing was pressed"
        target = named[0]
    else:
        # a prompt only the person grants in System Settings is never chosen without being named
        pressable = [p for p in now if (p.get("owner") or "UserNotificationCenter") not in SETTINGS_OWNERS]
        if not pressable:
            return False, ("the only prompt open is granted by the person in System Settings, "
                           "so nothing was pressed")
        target = pressable[0]
    owner = target.get("owner") or "UserNotificationCenter"
    if owner in SETTINGS_OWNERS and choice == "allow":
        return False, ("this prompt is granted by the person in System Settings (Privacy & Security), "
                       "not by a button here, so nothing was pressed")
    if owner in AX_OWNERS or by_title:
        title = BUTTONS.get(owner, BUTTONS["UserNotificationCenter"])[0 if choice == "allow" else 1]
        for attempt in range(1, max(1, tries) + 1):
            ok, said = (press or press_button)(target["pid"], target["wid"], title)
            if not ok:
                return False, (f"nothing was pressed: {said}" if attempt == 1
                               else f"pressed {title}, the prompt stayed, and the next press failed: {said}")
            ok, said = _closed(find, target, poll_seconds, f"Pressed {title}.")
            if ok:
                return True, said if attempt == 1 else f"Pressed {title} (on try {attempt})."
        return False, f"pressed {title} {max(1, tries)} times, but the prompt is still open"
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
                opener: Callable | None = None, window: int | None = None) -> Result:
    """Answer the front consent prompt through a running station, which presses it with its own
    Accessibility grant (``{"type": "consent"}`` on ``/input``, master token only). For a process
    that has no such grant, such as a launchd job. The station's answer, or why it could not be
    asked."""
    import json   # noqa: PLC0415
    import urllib.error   # noqa: PLC0415
    import urllib.request   # noqa: PLC0415
    if choice not in ("allow", "deny", "read"):
        return False, f"unknown answer {choice!r}: allow, deny or read"
    req = urllib.request.Request(
        f"{station.rstrip('/')}/input", method="POST",
        data=json.dumps({"type": "consent", "choice": choice,
                         **({"prompt": int(window)} if window is not None else {})}).encode(),
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


# ----------------------------------------------------------------------------- who asked, and timeouts

#: the unified log's line that starts a request (the process asking is the TARGET app, which asks on
#: behalf of the program that sent it Apple Events, named in the identity block that follows)
_REQUEST = re.compile(r"^(\S+ \S+) +\w+ +(.+?)\[(\d+):[0-9a-f]+\] \[com\.apple\.TCC:access\] "
                      r"TCCAccessRequestIndirect: TCCAccessRequestIndirect with pid \d+: target_identity")
_IDENTITY = re.compile(r'kTCCCodeIdentityIdentifier = "([^"]+)"')
#: the line that answers a request, logged by the same process once the decision is known
_RESULT = re.compile(r"^\S+ \S+ +\w+ +.+?\[(\d+):[0-9a-f]+\] \[com\.apple\.TCC:access\] "
                     r"TCCAccessRequestIndirect_block_invoke: access request result was (\d+)")
#: TCC's reason for a denial that came from a prompt nobody answered
TIMEOUT_REASON = 9
#: the per-user consent database, read only
USER_TCC = "~/Library/Application Support/com.apple.TCC/TCC.db"
#: the system consent database, read only, which holds screen recording among others
SYSTEM_TCC = "/Library/Application Support/com.apple.TCC/TCC.db"
#: the consent service for recording the screen and audio
SCREEN = "kTCCServiceScreenCapture"


def requests(text: str) -> list[dict]:
    """Pure. The consent requests in ``log show --style compact`` output, oldest first, as
    ``{at, target, target_pid, asker, result}``: ``target`` is the app asked to be controlled (the
    process that logged the request), ``asker`` the program that sent it Apple Events (an executable
    path or a bundle id), and ``result`` the answer the same process logged next (None while it has
    none, which is a request still waiting on a prompt). The log names both programs while the
    screen is locked, when the prompt's words cannot be read."""
    out: list[dict] = []
    current: dict | None = None
    for line in (text or "").splitlines():
        m = _REQUEST.match(line)
        if m:
            current = {"at": m.group(1), "target": m.group(2), "target_pid": int(m.group(3)), "asker": None,
                       "result": None}
            out.append(current)
            continue
        res = _RESULT.match(line)
        if res:
            waiting = [r for r in out if r["target_pid"] == int(res.group(1)) and r["result"] is None]
            if waiting:
                waiting[0]["result"] = int(res.group(2))
            continue
        if current is not None and current["asker"] is None:
            ident = _IDENTITY.search(line)
            if ident:
                current["asker"] = ident.group(1)
                current = None
    return [r for r in out if r["asker"]]


def waiting(found: list[dict]) -> list[dict]:
    """Pure. The requests among `found` that no answer followed yet: the ones a prompt is up for."""
    return [r for r in found if r.get("result") is None]


def short(name: str | None) -> str | None:
    """Pure. A program's short name: the last part of an executable path, or the bundle id as it is."""
    if not name:
        return None
    return name.rstrip("/").rsplit("/", 1)[-1] if "/" in name else name


def recent_requests(minutes: int = 10, *, run=None, env: Mapping[str, str] | None = None) -> list[dict] | None:
    """The consent requests the unified log holds from the last `minutes`, oldest first (``requests``),
    or None when the log could not be read."""
    argv = ["/usr/bin/log", "show", "--info", "--style", "compact", "--last", f"{int(minutes)}m", "--predicate",
            'eventMessage CONTAINS "TCCAccessRequestIndirect"']
    try:
        r = (run or _run)(argv, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return requests(r.stdout) if r.returncode == 0 else None


def timed_out(db: str | os.PathLike | None = None) -> list[dict] | None:
    """The Apple Events consents macOS recorded as denied because nobody answered the prompt in time,
    newest first, as ``{asker, target, at}`` (``at`` in seconds since the epoch), read only from the
    user's consent database. Until a new prompt is answered, the asker's every Apple Event to the
    target fails (error -1743) with no prompt and no message. None when the database cannot be read
    (it needs Full Disk Access). Nothing here writes the database."""
    import sqlite3   # noqa: PLC0415
    path = os.path.expanduser(str(db or USER_TCC))
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT client, indirect_object_identifier, last_modified FROM access "
                "WHERE service = 'kTCCServiceAppleEvents' AND auth_value = 0 AND auth_reason = ? "
                "ORDER BY last_modified DESC", (TIMEOUT_REASON,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return [{"asker": r[0], "target": r[1], "at": int(r[2] or 0)} for r in rows]


def granted(service: str, dbs: list[str | os.PathLike] | None = None) -> list[dict] | None:
    """The programs macOS has allowed `service` (for example ``SCREEN``), newest first, as
    ``{client, at}`` (``client`` an executable path or a bundle id, ``at`` in seconds since the epoch),
    read only from the system and the user's consent databases (or `dbs`). A prompt that asks again for
    a consent its program already holds is macOS confirming the grant: answering "Don't Allow" there
    takes it away. None when no database can be read (they need Full Disk Access). Nothing here writes
    a database."""
    import sqlite3   # noqa: PLC0415
    out: list[dict] = []
    read = False
    for db in dbs or [SYSTEM_TCC, USER_TCC]:
        path = os.path.expanduser(str(db))
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                rows = conn.execute("SELECT client, last_modified FROM access WHERE service = ? AND auth_value = 2",
                                    (service,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        read = True
        out += [{"client": r[0], "at": int(r[1] or 0)} for r in rows]
    return sorted(out, key=lambda g: -g["at"]) if read else None
