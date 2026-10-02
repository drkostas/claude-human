"""Put a window into the state a task needs before a person is handed it.

Two tasks can share one window (two System Settings panes, for example), so whichever opened last
would win, and "open the pane" is not "show the item": a long list opens scrolled to the wrong
place. A prepare is therefore a recipe, a list of steps run before the person arrives.

    activate            raise the app. A keystroke goes to the frontmost app, not to the window
                        that was captured, so typing without this lands in another app.
    open <url|app>      open a settings pane, an app, a document or a file:// folder, always with
                        `open -g` so nothing is raised. For an http(s) url in a Chromium browser it
                        makes a new window instead, so the url does not become a tab in the window
                        the person is using.
    wait <seconds>      panes animate, and a click 200ms early lands on the view that is leaving.
    search <text>       click the app's search field and type. This reaches an item in a long list
                        without depending on the scroll position. The field position is known for
                        System Settings (SEARCH_AT).
    type <text>         type into whatever has focus.
    key <name>          return, tab, esc, down, up, left, right, space, delete.
    click <x> <y>       normalised inside the window, 0 to 1.
    scroll <dx> <dy>    pixels. Positive dy scrolls up.

A recipe runs unattended, and its failures are invisible to the person waiting, so the runner is
careful. The app is opened and waited for (a window, not only a launch). Every step is tried twice.
A step that still fails stops the recipe and the log names it. The end checks that the window is
still there, because a recipe that ran while the app quit would otherwise report every step ok.
An input that the Mac refused (locked, a dialog holding focus, the window not on the visible
desktop) is a failed step and never a quiet success.

Steps come in two phases. Placement steps (open, wait) touch no input and can run on a desktop
nobody is looking at. Input steps (activate, type, key, click, scroll, search) need the window to be
frontmost on the visible desktop, because macOS sends a synthetic key to the frontmost app and a
synthetic click to whatever is topmost at that point. Running an input step against a window on
another desktop types into whatever the person is doing. So ``run(..., phase="place")`` runs only
placement steps and logs the input steps as deferred, and ``phase="input"`` runs the rest later.

"Is the app there" also has two answers. The on-screen window list sees only the visible desktop,
which is right for input and wrong for placement: a window placed on another desktop reads as
absent, the runner opens the app again, and a second window appears in front of the person. The
place phase therefore asks ``all_windows`` (every desktop, from yabai when it is installed) and
whether the process is running.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

KEYS = {"return": 36, "tab": 48, "esc": 53, "escape": 53, "down": 125, "up": 126,
        "left": 123, "right": 124, "space": 49, "delete": 51}

#: Where an app's search field is, in points from the window's top left corner. A fraction of the
#: window would drift when the window is resized, because the field sits a fixed distance below the
#: title bar. Measured on macOS 26.
SEARCH_AT = {"System Settings": (132, 75)}

#: Browsers that get a window of their own for an http(s) url. A browser puts `open <url>` into a
#: tab of whatever window it likes, usually the one the person is reading.
NEW_WINDOW_BROWSERS = frozenset({"Brave Browser", "Google Chrome", "Chromium"})

#: How long a browser may take to answer AppleScript. Short on purpose: the usual cause of no answer
#: is a JavaScript dialog in one of its windows, which blocks every AppleScript call to the browser
#: and does not clear on its own.
BROWSER_TIMEOUT = 6.0

PLACEMENT_VERBS = frozenset({"open", "wait"})
INPUT_VERBS = frozenset({"activate", "type", "key", "click", "scroll", "search"})
PHASES = ("place", "input", "all")


def phase_of(step: str) -> str:
    """"place", "input", or "skip" for a blank line or a comment."""
    s = step.strip()
    if not s or s.startswith("#"):
        return "skip"
    return "input" if s.partition(" ")[0].lower() in INPUT_VERBS else "place"


def succeeded(log: list[str]) -> bool:
    """True when the recipe left a prepared window, which is when the last line is
    "<app>: still open". Every path that gives up ends on a different line. A retry that then
    worked still ends on "still open", so it counts as success."""
    return bool(log) and log[-1].endswith(": still open")


def argv_phase(argv: list[str]) -> tuple[str, list[str]]:
    """Take ``--phase place|input|all`` out of an argv wherever it is, and return it with the rest.

    A caller may insert the flag into a recipe it did not write, so it is not read by position. An
    unknown value is refused, never read as "all": a typo that ran the input steps on a desktop
    nobody was looking at is the failure the phases exist to prevent."""
    rest, phase, i = [], "all", 0
    while i < len(argv):
        if argv[i] == "--phase" and i + 1 < len(argv):
            phase = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    if phase not in PHASES:
        raise SystemExit(f"--phase must be place, input or all, not {phase!r}")
    return phase, rest


def yabai_windows(yabai: str = "yabai") -> list[dict]:
    """Every window on every desktop, from yabai, as ``{id, space, app, title}``.

    Raises when yabai fails or answers something that is not a list. An empty answer is not an empty
    machine (at a locked screen yabai can exit 0 with a cut off answer)."""
    r = subprocess.run([yabai, "-m", "query", "--windows"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"yabai query --windows: {r.stderr.strip()[:200] or 'failed'}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"yabai query --windows: unreadable answer {r.stdout.strip()[:20]!r}") from None
    if not isinstance(data, list):
        raise RuntimeError("yabai query --windows: not a list")
    return [{"id": w.get("id"), "space": w.get("space"), "app": w.get("app") or "",
             "title": w.get("title") or ""} for w in data]


def default_screen() -> Any:
    """The on-screen window list from ``claude_human.screenshot`` and the default input sink."""
    from .. import screenshot  # noqa: PLC0415
    from .input import QuartzInput  # noqa: PLC0415
    return SimpleNamespace(windows=screenshot.windows, do_input=QuartzInput().do_input)


class Preparer:
    """Runs recipes. Every outside dependency is passed in, so the logic can be tested without a Mac.

    - ``screen`` answers ``windows()`` (the visible desktop) and ``do_input(msg)``.
    - ``all_windows`` lists the windows on every desktop. The default is yabai when it is on the
      PATH, and otherwise the on-screen list (then the place phase cannot see other desktops).
    - ``run`` is ``subprocess.run`` or a stand-in, and ``clock`` has ``time()`` and ``sleep()``.
    """

    def __init__(self, screen: Any = None, *, all_windows: Optional[Callable[[], list]] = None,
                 run: Optional[Callable[..., Any]] = None, clock: Any = None,
                 browser_timeout: float = BROWSER_TIMEOUT, search_at: Optional[dict] = None):
        self.screen = screen if screen is not None else default_screen()
        if all_windows is None:
            all_windows = yabai_windows if shutil.which("yabai") else self.screen.windows
        self.all_windows = all_windows
        self.sh = run or subprocess.run
        self.clock = clock or time
        self.browser_timeout = browser_timeout
        self.search_at = SEARCH_AT if search_at is None else search_at

    # ------------------------------------------------------------------ the window

    def search_point(self, app: str) -> tuple[float, float]:
        """The search field's point offset, as a fraction of the window as it is right now."""
        at = self.search_at.get(app)
        if at is None:
            raise ValueError(f"no search field known for {app!r}")
        win = next((w for w in self.screen.windows() if w.get("app") == app and w.get("width")), None)
        if not win:
            raise ValueError(f"{app} has no window to search in")
        return (at[0] / float(win["width"]), at[1] / float(win["height"]))

    def activate(self, app: str) -> None:
        self.sh(["/usr/bin/osascript", "-e", f'tell application "{app}" to activate'],
                check=False, timeout=10)
        self.clock.sleep(0.45)

    def present(self, app: str, phase: str = "all") -> bool:
        """Has the app a window: on any desktop (place phase) or on the visible one (otherwise)."""
        wins = self.all_windows() if phase == "place" else self.screen.windows()
        return any(w["app"] == app for w in wins)

    has_window = present

    def running(self, app: str) -> bool:
        """Is the app's process alive. `pgrep -x` matches a bundled app's own name, spaces and all,
        and needs no Automation permission."""
        r = self.sh(["/usr/bin/pgrep", "-x", app], check=False, timeout=10,
                    capture_output=True, text=True)
        return r.returncode == 0

    def ensure_running(self, app: str, timeout: float = 12.0, phase: str = "all") -> bool:
        """The app is there to be driven.

        In the place phase the test is the process and the launch is `open -g -a`, in the
        background. The phase's own `open` step makes the window, and a window made here as well
        would leave two, which a caller that identifies "the new window" cannot tell apart."""
        if phase == "place":
            if self.running(app):
                return True
            self.sh(["/usr/bin/open", "-g", "-a", app], check=False, timeout=15)
            deadline = self.clock.time() + timeout
            while self.clock.time() < deadline:
                self.clock.sleep(0.5)
                if self.running(app):
                    self.clock.sleep(0.8)
                    return True
            return False
        if self.present(app, phase):
            return True
        self.sh(["/usr/bin/open", "-a", app], check=False, timeout=15)
        deadline = self.clock.time() + timeout
        while self.clock.time() < deadline:
            self.clock.sleep(0.5)
            if self.present(app, phase):
                self.clock.sleep(0.8)
                return True
        return False

    def _ready(self, app: str, phase: str) -> bool:
        return self.running(app) if phase == "place" else self.present(app, phase)

    # ------------------------------------------------------------------ the steps

    def browser_window(self, app: str, url: str) -> None:
        """A new browser window showing `url`, without raising the browser. The url is set on the
        window this script made, never on "front window", which races the person's own clicks."""
        try:
            self.sh(["/usr/bin/osascript", "-e",
                     f'tell application "{app}"\n'
                     f'  set w to make new window\n'
                     f'  set URL of active tab of w to "{url}"\n'
                     f'end tell'], check=False, timeout=self.browser_timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"{app} did not answer AppleScript within {self.browser_timeout:g}s. A dialog in "
                f"one of its windows (a site asking permission, a 'leave site?' confirm) blocks every "
                f"AppleScript it is sent. Dismiss it and this places normally.") from None

    def act(self, msg: dict, what: str) -> None:
        """Send one input and raise when the Mac refused it."""
        r = self.screen.do_input(msg) or {}
        if not r.get("ok"):
            why = (r.get("detail")
                   or ("the Mac is locked" if r.get("locked") else None)
                   or ("a system dialog is taking input" if r.get("blocked") else None)
                   or "refused")
            raise RuntimeError(f"{what}: {why}")

    def step(self, app: str, verb: str, rest: str, settle: float) -> None:
        if verb == "activate":
            self.activate(app)
        elif verb == "open":
            target = rest.strip()
            if app in NEW_WINDOW_BROWSERS and target.startswith(("http://", "https://")):
                self.browser_window(app, target)
            else:
                # Anything that is not a url is opened as an app (`-a`), so a bare path keeps
                # meaning an app and a folder is written as a file:// url. `-g` keeps the person's
                # screen where it is: without it `open` raises the app.
                is_url = target.startswith(("x-apple", "http", "file://"))
                self.sh(["/usr/bin/open", "-g"] + ([target] if is_url else ["-a", target]),
                        check=False, timeout=15)
            self.clock.sleep(settle)
        elif verb == "wait":
            self.clock.sleep(min(float(rest or 1), 10))
        elif verb == "type":
            self.activate(app)
            self.act({"type": "text", "text": rest}, "type")
            self.clock.sleep(0.25)
        elif verb == "key":
            self.activate(app)
            code = KEYS.get(rest.strip().lower())
            if code is None:
                raise ValueError(f"unknown key {rest!r}")
            self.act({"type": "key", "code": code}, f"key {rest.strip()}")
            self.clock.sleep(0.25)
        elif verb == "click":
            x, y = (float(v) for v in rest.split()[:2])
            self.act({"type": "click", "app": app, "x": x, "y": y}, "click")
            self.clock.sleep(settle)
        elif verb == "scroll":
            dx, dy = (int(v) for v in rest.split()[:2])
            self.act({"type": "scroll", "dx": dx, "dy": dy}, "scroll")
            self.clock.sleep(0.3)
        elif verb == "search":
            self.activate(app)
            sx, sy = self.search_point(app)
            self.act({"type": "click", "app": app, "x": sx, "y": sy}, "search: focus the field")
            self.clock.sleep(0.35)
            # clear what the last task searched for, or the two queries join and match nothing
            self.act({"type": "key", "code": 0, "flags": 1048576}, "search: select all")  # Cmd+A
            self.clock.sleep(0.15)
            self.act({"type": "text", "text": rest}, "search: type the query")
            self.clock.sleep(settle)
        else:
            raise ValueError(f"unknown step {verb!r}")

    # ------------------------------------------------------------------ the recipe

    def run(self, app: str, steps: list[str], settle: float = 0.6, _retry: bool = True,
            phase: str = "all") -> list[str]:
        """Run a recipe and return what happened, one line per step.

        The window is checked before every step, not once at the top. A window that closed half way
        would otherwise take the rest of the clicks at coordinates that now belong to whatever is
        behind it."""
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, not {phase!r}")
        log: list[str] = []
        if not self.ensure_running(app, phase=phase):
            return [f"{app}: never started" if phase == "place" else f"{app}: never opened a window"]
        log.append(f"{app}: process running" if phase == "place" else f"{app}: window present")
        for raw in steps:
            step = raw.strip()
            if not step or step.startswith("#"):
                continue
            want = phase_of(step)
            if phase != "all" and want != phase:
                log.append(f"{step}: deferred to the {want} phase")
                continue
            if not self._ready(app, phase):
                log.append(f"{app}: {'quit' if phase == 'place' else 'window went away'} — reopening")
                if not self.ensure_running(app, phase=phase):
                    log.append(f"{app}: could not reopen it")
                    return log
            verb, _, rest = step.partition(" ")
            for attempt in (1, 2):
                try:
                    self.step(app, verb.lower(), rest, settle)
                    log.append(f"{step}: ok")
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 1:
                        self.clock.sleep(0.8)
                        continue
                    log.append(f"{step}: {type(e).__name__}: {e}")
                    return log
        if self.has_window(app, phase):
            log.append(f"{app}: still open")
            return log
        if _retry:
            log.append(f"{app}: window disappeared at the end — starting over once")
            return log + self.run(app, steps, settle, _retry=False, phase=phase)
        log.append(f"{app}: WINDOW DISAPPEARED")
        return log


def run(app: str, steps: list[str], settle: float = 0.6, phase: str = "all", **deps: Any) -> list[str]:
    """Run a recipe with a ``Preparer`` built from ``deps`` (see the class for what they are)."""
    return Preparer(**deps).run(app, steps, settle, phase=phase)
