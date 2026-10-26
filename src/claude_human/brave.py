"""Work in the person's own signed-in browser: run JavaScript on a page in a window of its own, and
list the browser's tabs quickly.

An assistant often needs a page the person is signed in to. Their own browser has the cookies and the
saved passwords, a throwaway automation profile has none. `run` opens a new window (never one of
theirs), loads a page, waits until it is ready, runs a script and returns what it set, then closes the
window. Chromium browsers that AppleScript can drive work (Brave is the default; pass `app`).

- `keep_open` leaves the window for a step the person finishes (a code, a captcha). With a `key` (by
  default the tmux session this runs in, which names the caller), the same caller's next `keep_open`
  run reuses that window while the browser still has it, so a flow of several steps keeps one window.
- The window's yabai id is taken when it is made (the one yabai lists afterwards and not before), so
  a window manager can move it at once. Matching windows by their position cannot tell apart windows
  the browser opens at one size and place.
- `list_tabs` asks each window for its tab ids, addresses and titles in three calls. Asking tab by tab
  takes about a minute for a thousand tabs; this takes under a second.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

APP = "Brave Browser"
STATE = Path(os.environ.get("CLAUDE_HUMAN_STATE", "~/.local/state/claude-human")).expanduser() / "brave"
YABAI = "/opt/homebrew/bin/yabai"
SEP = "\t"
READY = '!/^(chrome|about|brave):/.test(location.href) && document.readyState==="complete"'
#: where a script leaves its answer
RESULT = "__claude_r"


def osa(script: str, timeout: float = 30, *, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """(exit code, output or error text) of an AppleScript. A timeout is (-1, "")."""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return -1, ""
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


def _list_script(app: str, window: str | None = None) -> str:
    target = f"{{window id {window}}}" if window else "(get windows)"
    return f'''set sep to character id 9
set out to ""
tell application "{app}"
  repeat with w in {target}
    try
      set wid to id of w as text
      set ids to id of tabs of w
      set urls to URL of tabs of w
      set titles to title of tabs of w
      repeat with i from 1 to count of ids
        set out to out & wid & sep & ((item i of ids) as text) & sep & (item i of urls) & sep & (item i of titles) & linefeed
      end repeat
    end try
  end repeat
end tell
return out'''


def parse_tabs(raw: str | None) -> list[dict] | None:
    """Pure. The listing's lines as {"window", "tab", "url", "title"}; None for no answer."""
    if raw is None:
        return None
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = (line.split(SEP) + ["", "", "", ""])[:4]
        out.append({"window": parts[0], "tab": parts[1], "url": parts[2], "title": parts[3]})
    return out


def list_tabs(app: str = APP, window: str | None = None, *, timeout: float = 30,
              env: Mapping[str, str] | None = None) -> list[dict] | None:
    """Every tab of the browser (or of one window). None when the browser did not answer."""
    rc, out = osa(_list_script(app, window), timeout, env=env)
    return parse_tabs(out) if rc == 0 else None


def window_ids(app: str = APP, *, env: Mapping[str, str] | None = None) -> set[str]:
    rc, out = osa(f'tell application "{app}" to get id of every window', 10, env=env)
    return {w.strip() for w in out.split(",") if w.strip().isdigit()} if rc == 0 else set()


def yabai_ids(app: str = APP, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> set[int] | None:
    """The yabai ids of the app's windows, or None when yabai does not answer (or is not installed)."""
    try:
        r = subprocess.run([yabai, "-m", "query", "--windows"], capture_output=True, text=True, timeout=5,
                           env=env)
        return {w["id"] for w in json.loads(r.stdout) if w.get("app") == app}
    except Exception:  # noqa: BLE001 - no yabai: the id is not known, never guessed
        return None


def new_window(app: str = APP, *, ids=None, yids=None, make=None, wait: float = 10.0,
               step: float = 0.2, env: Mapping[str, str] | None = None) -> tuple[str | None, int | None]:
    """Make a new browser window: (its id, its yabai id or None). The new window is the one listed
    after and not before; two new yabai windows at once are not told apart, so no yabai id is guessed.
    `ids`, `yids` and `make` stand in for the browser and yabai in a test."""
    ids = ids or (lambda: window_ids(app, env=env))
    yids = yids or (lambda: yabai_ids(app, env=env))
    make = make or (lambda: osa(f'tell application "{app}" to make new window', env=env))
    before, ybefore = ids(), yids()
    make()
    wid = None
    for _ in range(int(wait / step)):
        new = ids() - before
        if len(new) == 1:
            wid = new.pop()
            break
        time.sleep(step)
    if wid is None:
        return None, None
    yid = None
    if ybefore is not None:
        for _ in range(int(3 / step)):
            fresh = (yids() or set()) - ybefore
            if len(fresh) == 1:
                yid = fresh.pop()
                break
            if len(fresh) > 1:
                break
            time.sleep(step)
    return wid, yid


def running(app: str = APP, *, env: Mapping[str, str] | None = None) -> bool | None:
    """Whether the browser runs; None when that could not be asked."""
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-x", app], capture_output=True, text=True, timeout=10,
                           env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


#: where the browser keeps its profiles, for continue_where_left_off
PROFILES_ROOT = Path("~/Library/Application Support/BraveSoftware/Brave-Browser")


def continue_where_left_off(root: Path | str = PROFILES_ROOT) -> list[str]:
    """Make the next start reopen the last session without asking, in every profile under `root`:
    "continue where you left off" on, and the last exit marked normal. After an unclean exit a
    Chromium browser asks "Restore pages?" whatever its startup setting, because the profile still
    says it crashed. Only while the browser is not running: it rewrites these preferences when it
    quits. Returns the profiles changed."""
    base = Path(root).expanduser()
    changed = []
    try:
        names = sorted(n.name for n in base.iterdir() if n.name == "Default" or n.name.startswith("Profile "))
    except OSError:
        return changed
    for n in names:
        path = base / n / "Preferences"
        try:
            prefs = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        prefs.setdefault("session", {})["restore_on_startup"] = 1
        prof = prefs.setdefault("profile", {})
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(prefs))
        os.replace(tmp, path)
        changed.append(n)
    return changed


class Processes:
    """What restart touches, replaced by a fake in tests."""

    def __init__(self, app: str = APP, env: Mapping[str, str] | None = None):
        self.app, self.env = app, env

    def pids(self) -> list[int]:
        try:
            r = subprocess.run(["/usr/bin/pgrep", "-x", self.app], capture_output=True, text=True,
                               timeout=10, env=self.env)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [int(x) for x in r.stdout.split() if x.isdigit()]

    def signal(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def open(self) -> bool:
        try:
            return subprocess.run(["/usr/bin/open", "-a", self.app, "--args", "--restore-last-session"],
                                  capture_output=True, timeout=30, env=self.env).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def answers(self) -> bool:
        return list_tabs(self.app, timeout=20, env=self.env) is not None

    def prepare(self) -> list[str]:
        return continue_where_left_off()

    def sleep(self, s: float) -> None:
        time.sleep(s)


def restart(app: str = APP, *, quit_wait: float = 60.0, answer_wait: float = 90.0,
            procs: Processes | None = None, env: Mapping[str, str] | None = None) -> tuple[str, dict]:
    """Restart a browser that stopped answering, with its windows and tabs.

    It is asked to quit (SIGTERM, which a Chromium browser takes as a normal quit that saves the
    session) and stopped with SIGKILL when it has not quit after `quit_wait` seconds. Then its
    profiles are set to continue where they left off (continue_where_left_off) and it is opened
    with --restore-last-session, and the restart counts once it answers a tab listing again. A
    browser stopped by force reopens the session it saved last, so the pages opened since then can
    be missing: compare the tabs before and after.

    Returns (outcome, detail): "success" or "failure", with "message" and "how" ("quit" or
    "stopped")."""
    import signal   # noqa: PLC0415
    p = procs or Processes(app, env)
    pids = p.pids()
    if len(pids) != 1:
        return "failure", {"message": f"expected one {app} process, found {len(pids)}; nothing was stopped"}
    pid = pids[0]
    p.signal(pid, signal.SIGTERM)
    how = "quit"
    waited = 0.0
    while p.alive(pid):
        if waited >= quit_wait:
            p.signal(pid, signal.SIGKILL)
            how = "stopped"
            for _ in range(40):
                if not p.alive(pid):
                    break
                p.sleep(0.25)
            else:
                return "failure", {"message": f"{app} did not stop", "how": how}
            break
        p.sleep(1)
        waited += 1
    p.prepare()
    if not p.open():
        return "failure", {"message": f"{app} {how}, and could not be opened again", "how": how}
    waited = 0.0
    while not p.answers():
        if waited >= answer_wait:
            return "failure", {"message": f"{app} opened again but did not answer within {answer_wait:.0f} s",
                               "how": how}
        p.sleep(3)
        waited += 3
    return "success", {"message": f"{app} {how} and reopened its last session", "how": how}


def tabs(app: str = APP, *, timeout: float = 30, env: Mapping[str, str] | None = None) -> list[dict] | None:
    """Every tab. [] when the browser is not running, which is known: nothing is open. None when it
    runs and could not be asked, which is unknown, never "no tabs"."""
    up = running(app, env=env)
    if up is None:
        return None
    if not up:
        return []
    return list_tabs(app, timeout=timeout, env=env)


#: how many times close_tab reads the tabs back, and the pause between reads (the browser may list a
#: tab it was just told to close for a moment, most often when it was the window's last one)
CLOSE_READS, CLOSE_GAP = 4, 0.5


def close_tab(window: str, tab: str, app: str = APP, *, env: Mapping[str, str] | None = None,
              reads: int = CLOSE_READS, gap: float = CLOSE_GAP, sleep=time.sleep) -> str:
    """Close one tab by its id: "success" | "failure" | "unknown". Gone on a re-read is the only
    success, and a re-read that could not be taken is unknown. The tabs are read back up to `reads`
    times, `gap` seconds apart, before a tab still listed counts as a failure. There is no function
    here that closes a window: a window goes away by itself when its last tab closes."""
    if not (str(window).isdigit() and str(tab).isdigit()):
        return "failure"
    osa(f'tell application "{app}" to close (tab id {tab} of window id {window})', 10, env=env)
    for i in range(max(1, reads)):
        if i:
            sleep(gap)
        after = tabs(app, env=env)
        if after is None:
            return "unknown"
        if not any(t["tab"] == str(tab) for t in after):
            return "success"
    return "failure"


def active_tab(window: str, app: str = APP, *, env: Mapping[str, str] | None = None) -> str | None:
    """The id of a window's active tab, or None."""
    if not str(window).isdigit():
        return None
    rc, out = osa(f'tell application "{app}" to return (id of active tab of window id {window}) as text',
                  10, env=env)
    return out if rc == 0 and out.isdigit() else None


def front_window(app: str = APP, *, env: Mapping[str, str] | None = None) -> str | None:
    """The id of the browser's front window; None when it does not run or did not answer."""
    if not running(app, env=env):
        return None
    rc, out = osa(f'tell application "{app}" to return (id of window 1) as text', 10, env=env)
    return out if rc == 0 and out.isdigit() else None


def bring_forward(window: str, app: str = APP, *, hold: float = 0.5,
                  env: Mapping[str, str] | None = None) -> bool:
    """Make `window` the browser's last active window, the one a link clicked in another app opens
    in. Setting the window's index alone reorders the windows but is not what the browser counts as
    last active; the browser also has to come to the front, for about half a second (`hold`). Hand
    focus back to the app the person was in afterwards (claude_human.screenshot.activate). Nothing
    of this registers on a locked screen. True when the browser then reports `window` in front."""
    if not str(window).isdigit():
        return False
    osa(f'tell application "{app}" to set index of window id {int(window)} to 1\n'
        f'tell application "{app}" to activate', 10, env=env)
    time.sleep(hold)
    return front_window(app, env=env) == str(window)


def window_origin(window: str, app: str = APP, *,
                  env: Mapping[str, str] | None = None) -> tuple[float, float] | None:
    """A window's top-left corner, as the browser reports its bounds."""
    if not str(window).isdigit():
        return None
    rc, out = osa(f'tell application "{app}" to get bounds of window id {window}', 10, env=env)
    try:
        x, y, *_ = [float(v) for v in out.split(",")]
        return x, y
    except ValueError:
        return None


def window_name(window: str, app: str = APP, *, env: Mapping[str, str] | None = None) -> str | None:
    """The browser's name for its window (its active tab's title); yabai shows "<name> - Brave"."""
    if not str(window).isdigit():
        return None
    rc, out = osa(f'tell application "{app}" to get name of window id {window}', 10, env=env)
    return out if rc == 0 else None


def match_by_origin(origin: tuple[float, float], yabai_windows: list[dict], app: str = APP,
                    name: str | None = None) -> int | None:
    """Pure. The one yabai window of `app` at that corner, or None. When several share the corner
    (every window filled to the screen does), the window's own name tells them apart: yabai titles a
    Brave window "<name> - Brave". Still several, or none: None, never a guess."""
    hits = [w for w in yabai_windows if w.get("app") == app
            and round(w["frame"]["x"]) == round(origin[0])
            and round(w["frame"]["y"]) == round(origin[1])]
    if len(hits) > 1 and name:
        hits = [w for w in hits if (w.get("title") or "") in (name, f"{name} - Brave")
                or (len(name) >= 12 and (w.get("title") or "").startswith(name[:40]))]
    return hits[0]["id"] if len(hits) == 1 else None


def join_by_origin(items: list[dict], yabai_windows: list[dict], app: str = APP) -> dict[int, dict]:
    """Pure. {yabai id: item} for an app's own window list (each item has "origin" and "name"),
    joined to yabai's windows of that app by corner, then name, then elimination. yabai knows a
    window's title only while its desktop is showing, so two windows at one corner on hidden
    desktops cannot be told apart by name; once every other window is joined, a corner left with
    exactly one of each is that pair. Anything else stays unjoined."""
    joined: dict[int, dict] = {}
    left = []
    for it in items:
        wid = match_by_origin(it["origin"], yabai_windows, app, it.get("name"))
        if wid is not None and wid not in joined:
            joined[wid] = it
        else:
            left.append(it)
    free = [w for w in yabai_windows if w.get("app") == app and w.get("id") not in joined]

    def corner(xy) -> tuple[int, int]:
        return round(xy[0]), round(xy[1])
    for c in {corner(it["origin"]) for it in left}:
        mine = [it for it in left if corner(it["origin"]) == c]
        theirs = [w for w in free if corner((w["frame"]["x"], w["frame"]["y"])) == c]
        if len(mine) == 1 and len(theirs) == 1:
            joined[theirs[0]["id"]] = mine[0]
    return joined


def default_key(*, env: Mapping[str, str] | None = None) -> str | None:
    """The tmux session this runs in, which names the caller; None outside tmux."""
    if not (env if env is not None else os.environ).get("TMUX"):
        return None
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = r.stdout.strip()
    return name if name and all(ch.isalnum() or ch in "-_." for ch in name) else None


def kept(key: str, *, state: Path = STATE, open_ids: set[str] | None = None, app: str = APP) -> dict | None:
    """The window `key` kept last time, while the browser still has it: {"window", "yabai"} or None."""
    try:
        got = json.loads((state / f"{key}.json").read_text())
    except (OSError, ValueError):
        return None
    ids = window_ids(app) if open_ids is None else open_ids
    return got if str(got.get("window")) in ids else None


def keep(key: str, window: str, yabai: int | None, *, state: Path = STATE) -> None:
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / f"{key}.json.tmp"
    tmp.write_text(json.dumps({"window": window, "yabai": yabai}))
    os.replace(tmp, state / f"{key}.json")


def js_in(window: str, code: str, app: str = APP) -> str:
    esc = code.replace("\\", "\\\\").replace('"', '\\"')
    rc, out = osa(f'tell application "{app}" to execute active tab of (first window whose id is '
                  f'{window}) javascript "{esc}"')
    if rc != 0:
        raise RuntimeError(out)
    return out


def run(url: str, code: str, *, wait_for: str = READY, timeout: float = 40, keep_open: bool = False,
        window: str | None = None, key: str | None = None, app: str = APP,
        state: Path = STATE) -> dict:
    """Run `code` on `url` in a window of the browser. Returns {"result", "window", "yabai",
    "reused"}. The script may be async: it assigns its answer to window.__claude_r (a string); when it
    does not, the value of its last expression is the result. A window this opened is closed unless
    `keep_open`; one passed as `window` is never closed. Raises TimeoutError when the page or the
    script does not answer in `timeout` seconds.

    `wait_for` is a condition on the page, checked BEFORE the script runs (the default waits for the
    page to finish loading). The script's answer is waited for by itself. So a `wait_for` that names
    window.__claude_r can never hold, since only the script sets it: that is refused with ValueError
    rather than left to time out. Wait for something on the page instead, such as
    `document.querySelector(".comments")`."""
    if RESULT in (wait_for or ""):
        raise ValueError(f"wait_for is checked before the script runs, and only the script sets "
                         f"window.{RESULT}, so {wait_for!r} can never hold. Leave wait_for out (the "
                         f"answer is waited for by itself) or wait for something on the page")
    owned, yid, reused = window is None, None, False
    if owned:
        mine = kept(key, state=state, app=app) if (keep_open and key) else None
        if mine:
            window, yid, owned, reused = str(mine["window"]), mine.get("yabai"), False, True
        else:
            window, yid = new_window(app)
            if window is None:
                raise RuntimeError("could not identify the new browser window")
            if keep_open and key:
                keep(key, window, yid, state=state)
    try:
        if url != "-":
            osa(f'tell application "{app}" to set URL of active tab of (first window whose id is '
                f'{window}) to "{url}"')
        end = time.time() + timeout
        while True:
            try:
                if js_in(window, f"!!({wait_for})", app) == "true":
                    break
            except RuntimeError:
                pass
            if time.time() > end:
                raise TimeoutError(f"timed out waiting for: {wait_for}")
            time.sleep(0.5)
        js_in(window, "window.__claude_r = undefined;", app)
        last = js_in(window, code, app)
        end = time.time() + timeout
        while True:
            r = js_in(window, 'window.__claude_r === undefined ? "__PENDING__" : String(window.__claude_r)', app)
            if r != "__PENDING__":
                return {"result": r, "window": window, "yabai": yid, "reused": reused}
            if "window.__claude_r" not in code:
                return {"result": last, "window": window, "yabai": yid, "reused": reused}
            if time.time() > end:
                raise TimeoutError("timed out waiting for window.__claude_r")
            time.sleep(0.5)
    finally:
        if owned and not keep_open:
            osa(f'tell application "{app}" to close (first window whose id is {window})')


#: where Brave keeps each profile's saved logins
PROFILES = Path("~/Library/Application Support/BraveSoftware/Brave-Browser").expanduser()


def has_login(site: str, *, profiles: Path = PROFILES) -> bool | None:
    """Whether the browser's saved logins hold an entry for `site` (matched inside the address), in
    any profile: True, False, or None when no profile's login store could be read.

    Only the address column is read, from a copy of each store (the browser keeps it locked while it
    runs). The passwords there are encrypted with a key in the login keychain and are never touched,
    so this asks for no permission and shows nothing on screen."""
    import shutil   # noqa: PLC0415
    import sqlite3   # noqa: PLC0415
    import tempfile   # noqa: PLC0415
    if not site:
        raise ValueError("no site to look for")
    found, read = False, False
    for store in sorted(profiles.glob("*/Login Data")):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "logins"
            try:
                shutil.copyfile(store, copy)
                with sqlite3.connect(copy) as db:
                    n = db.execute("SELECT count(*) FROM logins WHERE origin_url LIKE ?",
                                   (f"%{site}%",)).fetchone()[0]
            except (OSError, sqlite3.Error):
                continue
        read = True
        found = found or n > 0
    return found if read else None


def main(argv: list[str]) -> int:
    """`brave URL JS_FILE [--wait-for EXPR] [--timeout S] [--keep-open] [--window ID] [--key KEY]
    [--app NAME]`: the result on stdout; with --keep-open, "WINDOW <id>[ YABAI <id>]" on stderr.
    `brave has-login SITE`: exit 0 when a saved login matches the site, 1 when none, 2 when the
    login stores could not be read."""
    a = list(argv)
    if a[:1] == ["has-login"]:
        if len(a) != 2:
            print("usage: brave has-login SITE", file=sys.stderr)
            return 2
        got = has_login(a[1])
        return 0 if got else (1 if got is False else 2)

    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a else default

    if len(a) < 2:
        print(main.__doc__, file=sys.stderr)
        return 2
    keep_open = "--keep-open" in a
    key = opt("--key") or (default_key() if keep_open else None)
    try:
        out = run(a[0], Path(a[1]).read_text(), wait_for=opt("--wait-for", READY),
                  timeout=float(opt("--timeout", 40)), keep_open=keep_open, window=opt("--window"),
                  key=key, app=opt("--app", APP))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    except (TimeoutError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    if keep_open:
        print(f"WINDOW {out['window']}" + (f" YABAI {out['yabai']}" if out["yabai"] else ""), file=sys.stderr)
    print(out["result"])
    return 0
