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


#: a script longer than this goes to osascript on its standard input rather than as an argument:
#: macOS holds all of a program's arguments to about 1 MB, and standard input has no such limit
ARG_MAX_SCRIPT = 100_000


#: the end of the run in progress (time.monotonic()), set by `run`: every AppleScript call made
#: during it is cut to the time left, so its timeout holds for the whole run even when the browser
#: answers nothing (each call alone would wait out its own 30 s)
_deadline: float | None = None


def _left(timeout: float) -> float:
    """`timeout`, cut to what is left of the run in progress (at least a tenth of a second)."""
    if _deadline is None:
        return timeout
    return max(0.1, min(timeout, _deadline - time.monotonic()))


def osa(script: str, timeout: float = 30, *, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """(exit code, output or error text) of an AppleScript. A timeout is (-1, "")."""
    long = len(script) > ARG_MAX_SCRIPT
    try:
        r = subprocess.run(["/usr/bin/osascript", "-"] if long else ["/usr/bin/osascript", "-e", script],
                           input=script if long else None, capture_output=True, text=True,
                           timeout=_left(timeout), env=env)
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
        r = subprocess.run([yabai, "-m", "query", "--windows"], capture_output=True, text=True, timeout=_left(5),
                           env=env)
        return {w["id"] for w in json.loads(r.stdout) if w.get("app") == app}
    except Exception:  # noqa: BLE001 - no yabai: the id is not known, never guessed
        return None


#: the owners of the prompts behind which every Apple Event waits until a person answers
#: (claude_human.consent): while one is up, no AppleScript to the browser returns
APPLE_EVENT_BLOCKERS = {"UserNotificationCenter"}


class Blocked(RuntimeError):
    """A prompt on screen holds every Apple Event to the browser, so nothing here can answer."""


def blocked_by() -> str | None:
    """The owner of a prompt on screen that holds every Apple Event to the browser, or None (also
    when the window list cannot be read)."""
    from . import consent   # noqa: PLC0415
    for p in consent.prompts() or []:
        if p.get("owner") in APPLE_EVENT_BLOCKERS:
            return p["owner"]
    return None


def _refuse_if_blocked(blocker=None) -> None:
    """Raise Blocked, naming the prompt's owner, when one is on screen."""
    owner = (blocker or blocked_by)()
    if owner:
        raise Blocked(f"blocked_by {owner}: a macOS prompt is blocking Brave (every Apple Event to the "
                      f"browser waits behind it until someone answers it; claude-human consent list)")


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
    # by the clock: each listing is an AppleScript call that can itself take its whole 30 s timeout,
    # so a count of polls once waited 50 times as long as `wait`
    end = time.monotonic() + (wait if _deadline is None else min(wait, max(0.0, _deadline - time.monotonic())))
    while True:
        new = ids() - before
        if len(new) == 1:
            wid = new.pop()
            break
        if time.monotonic() >= end:
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


def start(app: str = APP, *, answer_wait: float = 90.0, procs: Processes | None = None,
          env: Mapping[str, str] | None = None) -> tuple[str, dict]:
    """Open a browser that is not running with its last session, and wait until it answers.

    For a browser that something else ended (a crash, a signal to every process of the user):
    nothing is stopped, so nothing can be lost, and a running browser is left as it is ("skipped").
    Its profiles are set to continue where they left off and it is opened with
    --restore-last-session, as restart does. Returns (outcome, detail): "success" once it answers a
    tab listing, "skipped" when it was running, "failure" when it could not be opened or did not
    answer within `answer_wait` seconds; detail carries "message"."""
    p = procs or Processes(app, env)
    if p.pids():
        return "skipped", {"message": f"{app} is running; nothing was started"}
    p.prepare()
    if not p.open():
        return "failure", {"message": f"{app} could not be opened"}
    waited = 0.0
    while not p.answers():
        if waited >= answer_wait:
            return "failure", {"message": f"{app} opened but did not answer within {answer_wait:.0f} s"}
        p.sleep(3)
        waited += 3
    return "success", {"message": f"{app} was not running, and it reopened its last session"}


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


#: the most JavaScript one AppleScript call carries. A longer script (or a file for `upload`) is
#: sent in parts of at most this many characters, each checked as it lands, and put together in
#: the page. One call of this size takes about half a second
CHUNK = 4_000_000
#: the largest file `upload` sends unless the caller says otherwise
MAX_UPLOAD = 25 * 1024 * 1024
#: where sent text waits in the page until it is used
INBOX = "__claude_in"
#: CRC-32 of a string's UTF-16 code units, in the page (the same as zlib.crc32 of its UTF-16LE bytes)
_CRC_JS = ("function(s){var t=window.__claude_crc;if(!t){t=[];for(var n=0;n<256;n++){var c=n;"
           "for(var k=0;k<8;k++)c=c&1?0xEDB88320^(c>>>1):c>>>1;t[n]=c>>>0;}window.__claude_crc=t;}"
           "var c=0xFFFFFFFF;for(var i=0;i<s.length;i++){var u=s.charCodeAt(i);"
           "c=t[(c^u)&255]^(c>>>8);c=t[(c^(u>>>8))&255]^(c>>>8);}return ((c^0xFFFFFFFF)>>>0);}")


def _escape(code: str) -> str:
    return code.replace("\\", "\\\\").replace('"', '\\"')


def _js_once(window: str, code: str, app: str) -> str:
    rc, out = osa(f'tell application "{app}" to execute active tab of (first window whose id is '
                  f'{window}) javascript "{_escape(code)}"')
    if rc != 0:
        raise RuntimeError(out)
    return out


def _units(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass")


def _parts(text: str, chunk: int) -> list[str]:
    """Pure. `text` cut into parts whose escaped JSON literal is at most `chunk` characters."""
    out, i = [], 0
    while i < len(text):
        n = chunk
        while True:
            part = text[i:i + n]
            if len(_escape(json.dumps(part))) <= chunk or n == 1:
                break
            n = max(1, n // 2)
        out.append(part)
        i += len(part)
    return out


def _short(out: str) -> str:
    """An error text with any long run that could be sent data cut out, so no part of a file or a
    script reaches an exception or a log."""
    import re   # noqa: PLC0415
    return re.sub(r'[A-Za-z0-9+/=\\"]{40,}', "...", out)[:300]


def send(window: str, text: str, app: str = APP, *, chunk: int = CHUNK) -> str:
    """Put `text` into the page in parts, each small enough for one AppleScript call, and return
    the key it waits under: `window.__claude_in[key]`. Each part's arrival is checked by the length
    the page then holds, and the whole by its CRC-32, so a part that is lost, repeated or put in the
    wrong place raises RuntimeError rather than leaving a different text in the page. The caller
    uses the text and deletes it; a page that navigates away loses it."""
    import secrets   # noqa: PLC0415
    import zlib   # noqa: PLC0415
    key = secrets.token_hex(6)
    slot = f'window.{INBOX}["{key}"]'
    parts = _parts(text, chunk)
    _js_once(window, f'(window.{INBOX}=window.{INBOX}||{{}})["{key}"]="";"0"', app)
    held = 0
    try:
        for i, part in enumerate(parts):
            held += len(_units(part)) // 2
            try:
                got = _js_once(window, f"{slot}+={json.dumps(part)};String({slot}.length)", app)
            except RuntimeError as e:
                raise RuntimeError(f"part {i + 1} of {len(parts)} did not reach the page: "
                                   f"{_short(str(e))}") from None
            if got != str(held):
                raise RuntimeError(f"part {i + 1} of {len(parts)} did not arrive whole: the page holds "
                                   f"{_short(got)} characters, {held} were sent")
        want = zlib.crc32(_units(text))
        got = _js_once(window, f"String(({_CRC_JS})({slot}))", app)
        if got != str(want):
            raise RuntimeError(f"the {len(parts)} parts arrived but differ from what was sent "
                               f"(CRC-32 {_short(got)} in the page, {want} sent)")
    except BaseException:
        drop(window, key, app)
        raise
    return key


def drop(window: str, key: str, app: str = APP) -> None:
    """Delete what `send` left in the page under `key`. Never raises."""
    try:
        _js_once(window, f'window.{INBOX}&&delete window.{INBOX}["{key}"];"ok"', app)
    except RuntimeError:
        pass


def js_in(window: str, code: str, app: str = APP, *, chunk: int = CHUNK) -> str:
    """Run `code` in the active tab of `window` and return the value of its last expression, as
    text. A script longer than one AppleScript call can carry is sent in parts (`send`), checked,
    and then run with an indirect eval, which gives it the same global scope and the same result.

    The script runs in the browser's isolated world for AppleScript: it shares the page's DOM but
    not its JavaScript. The page's own functions and libraries are not visible from it; reach them
    by adding a `<script>` element to the page."""
    if len(_escape(code)) <= chunk:
        return _js_once(window, code, app)
    key = send(window, code, app, chunk=chunk)
    return _js_once(window, f'(function(k){{var s=window.{INBOX}[k];delete window.{INBOX}[k];'
                            f'return (0,eval)(s);}})("{key}")', app)


def _settled(read, *, settle: float, timeout: float, step: float = 0.5, clock=None, sleep=None) -> str:
    """The value `read()` gives once it is not empty and has stayed the same for `settle` seconds.
    TimeoutError after `timeout` seconds, naming the last value."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    end = clock() + timeout
    last, since = None, clock()
    while True:
        now = read()
        if now != last:
            last, since = now, clock()
        elif now and clock() - since >= settle:
            return now
        if clock() > end:
            raise TimeoutError(f"the upload did not settle within {timeout:.0f} s "
                               f"(last value {_short(last or '')!r})")
        sleep(step)


def upload(window: str, selector: str, paths, *, app: str = APP, done: str | None = None,
           settle: float = 4.0, timeout: float = 120, max_bytes: int = MAX_UPLOAD,
           names: list[str] | None = None, types: list[str] | None = None,
           chunk: int = CHUNK) -> dict:
    """Put one file or several into the page's file input, as if the person had chosen them.

    `selector` is a CSS selector for an `<input type=file>` in the active tab of `window`. `paths`
    is one path or a list. Each file is sent into the page in checked parts (`send`), becomes a
    File there (named `names[i]` or the file's own name, of type `types[i]` or the type its
    extension suggests), and the files are set on the input together through a DataTransfer. Then
    the input fires `input` and `change`, as it does for a person's choice, and the page's own
    upload starts.

    `done` is a JavaScript expression the page fills when its upload has finished (a file name
    field, an uploaded-list entry). When given, this waits until its value is not empty and has
    stayed the same for `settle` seconds: some pages upload more than once per change. TimeoutError
    after `timeout` seconds.

    Refused with ValueError before anything is sent: a missing file, a file larger than
    `max_bytes`, several files for an input without `multiple`, a selector that names no file input
    or a disabled one. File contents are never logged and never put in an error.

    Returns {"files": [names], "bytes": [sizes], "done": the settled value or None}."""
    import base64   # noqa: PLC0415
    import mimetypes   # noqa: PLC0415
    files = [Path(paths)] if isinstance(paths, (str, os.PathLike)) else [Path(p) for p in paths]
    if not files:
        raise ValueError("no file to upload")
    names = list(names) if names else [f.name for f in files]
    types = list(types) if types else [mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                                       for f in files]
    if len(names) != len(files) or len(types) != len(files):
        raise ValueError("give one name and one type per file")
    sizes = []
    for f in files:
        if not f.is_file():
            raise ValueError(f"{f} is not a file")
        size = f.stat().st_size
        if size > max_bytes:
            raise ValueError(f"{f.name} is {size} bytes, over the {max_bytes}-byte limit; pass a larger "
                             f"max_bytes if the page takes it")
        sizes.append(size)
    sel = json.dumps(selector)
    state = _js_once(window, f"(function(){{var e=document.querySelector({sel});"
                             "if(!e)return 'missing';if(e.tagName!=='INPUT'||e.type!=='file')return 'not-file';"
                             "if(e.disabled)return 'disabled';return e.multiple?'multiple':'single';})()", app)
    why = {"missing": f"no element matches {selector!r}",
           "not-file": f"{selector!r} is not an <input type=file>",
           "disabled": f"the file input {selector!r} is disabled"}.get(state)
    if why:
        raise ValueError(why)
    if state != "multiple" and len(files) > 1:
        raise ValueError(f"the file input {selector!r} takes one file (it has no multiple attribute), "
                         f"{len(files)} were given")
    if state not in ("single", "multiple"):
        raise RuntimeError(f"could not read the file input: {_short(state)}")
    keys: list[str] = []
    try:
        for f in files:
            keys.append(send(window, base64.b64encode(f.read_bytes()).decode("ascii"), app, chunk=chunk))
        spec = json.dumps([{"key": k, "name": n, "type": t} for k, n, t in zip(keys, names, types)])
        got = _js_once(window, f"(function(sel,spec){{var e=document.querySelector(sel);"
                               "if(!e||e.type!=='file')return 'missing';var dt=new DataTransfer();"
                               f"spec.forEach(function(s){{var raw=atob(window.{INBOX}[s.key]);"
                               "var b=new Uint8Array(raw.length);for(var i=0;i<raw.length;i++)b[i]=raw.charCodeAt(i);"
                               "dt.items.add(new File([b],s.name,{type:s.type}));});"
                               "e.files=dt.files;e.dispatchEvent(new Event('input',{bubbles:true}));"
                               "e.dispatchEvent(new Event('change',{bubbles:true}));"
                               f"return String(e.files.length);}})({sel},{spec})", app)
    finally:
        for k in keys:
            drop(window, k, app)
    if got != str(len(files)):
        raise RuntimeError(f"the files were sent but the input holds {_short(got)} of {len(files)}")
    value = None
    if done:
        probe = (f"(function(){{var v=({done});return v===undefined||v===null||v===false?'':String(v);}})()")
        value = _settled(lambda: _js_once(window, probe, app), settle=settle, timeout=timeout)
    return {"files": names, "bytes": sizes, "done": value}


def run(url: str, code: str, *, wait_for: str = READY, timeout: float = 40, keep_open: bool = False,
        window: str | None = None, key: str | None = None, app: str = APP,
        state: Path = STATE, blocker=None) -> dict:
    """Run `code` on `url` in a window of the browser. Returns {"result", "window", "yabai",
    "reused"}. The script may be async: it assigns its answer to window.__claude_r (a string); when it
    does not, the value of its last expression is the result. A window this opened is closed unless
    `keep_open`; one passed as `window` is never closed. Raises TimeoutError when the page or the
    script does not answer in `timeout` seconds.

    `wait_for` is a condition on the page, checked BEFORE the script runs (the default waits for the
    page to finish loading). The script's answer is waited for by itself. So a `wait_for` that names
    window.__claude_r can never hold, since only the script sets it: that is refused with ValueError
    rather than left to time out. Wait for something on the page instead, such as
    `document.querySelector(".comments")`.

    A macOS prompt that holds every Apple Event (a consent prompt, `blocked_by`) raises Blocked at
    once, before a window is opened, and in either wait when it appears during the run: behind one,
    each AppleScript call waits out its own timeout and the run went on for minutes. `blocker`
    stands in for `blocked_by` in a test.

    `timeout` holds for the whole run, opening the window included: every AppleScript call is cut to
    the time left, and only closing the window may take up to CLOSE_GRACE seconds more."""
    if RESULT in (wait_for or ""):
        raise ValueError(f"wait_for is checked before the script runs, and only the script sets "
                         f"window.{RESULT}, so {wait_for!r} can never hold. Leave wait_for out (the "
                         f"answer is waited for by itself) or wait for something on the page")
    _refuse_if_blocked(blocker)
    global _deadline
    outer, _deadline = _deadline, time.monotonic() + timeout
    try:
        return _run_within(url, code, wait_for, timeout, keep_open, window, key, app, state, blocker)
    finally:
        _deadline = outer


def _ran_out(blocker, what: str, timeout: float) -> None:
    """Raise Blocked when a prompt is on screen, else TimeoutError naming `what`."""
    _refuse_if_blocked(blocker)
    raise TimeoutError(f"timed out after {timeout:g} s waiting for {what} (Brave did not answer in time)")


#: how long closing the run's own window may take after its time ran out
CLOSE_GRACE = 5.0


def _run_within(url, code, wait_for, timeout, keep_open, window, key, app, state, blocker) -> dict:
    """`run`'s body, under its deadline."""
    global _deadline
    owned, yid, reused = window is None, None, False
    if owned:
        mine = kept(key, state=state, app=app) if (keep_open and key) else None
        if mine:
            window, yid, owned, reused = str(mine["window"]), mine.get("yabai"), False, True
        else:
            window, yid = new_window(app)
            if window is None:
                if time.monotonic() >= _deadline:
                    _ran_out(blocker, "a new browser window", timeout)
                raise RuntimeError("could not identify the new browser window")
            if keep_open and key:
                keep(key, window, yid, state=state)
    try:
        if url != "-":
            osa(f'tell application "{app}" to set URL of active tab of (first window whose id is '
                f'{window}) to "{url}"')
        while True:
            try:
                if js_in(window, f"!!({wait_for})", app) == "true":
                    break
            except RuntimeError:
                _refuse_if_blocked(blocker)
            if time.monotonic() >= _deadline:
                _ran_out(blocker, wait_for, timeout)
            time.sleep(0.5)
        js_in(window, "window.__claude_r = undefined;", app)
        last = js_in(window, code, app)
        while True:
            r = js_in(window, 'window.__claude_r === undefined ? "__PENDING__" : String(window.__claude_r)', app)
            if r != "__PENDING__":
                return {"result": r, "window": window, "yabai": yid, "reused": reused}
            if "window.__claude_r" not in code:
                return {"result": last, "window": window, "yabai": yid, "reused": reused}
            if time.monotonic() >= _deadline:
                _ran_out(blocker, "window.__claude_r", timeout)
            time.sleep(0.5)
    finally:
        # behind a prompt the close would only wait out its own timeout too; past the deadline it
        # gets CLOSE_GRACE seconds of its own
        if owned and not keep_open and not (blocker or blocked_by)():
            _deadline = None
            osa(f'tell application "{app}" to close (first window whose id is {window})', CLOSE_GRACE)


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
    login stores could not be read.
    `brave upload WINDOW SELECTOR FILE... [--done EXPR] [--settle S] [--timeout S] [--max-bytes N]
    [--app NAME]`: put the files into that file input; the result as JSON on stdout, exit 2 when
    refused before sending, 1 when the upload failed or did not settle."""
    a = list(argv)
    if a[:1] == ["upload"]:
        return _upload_main(a[1:])
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


def _upload_main(a: list[str]) -> int:
    opts, rest, i = {}, [], 0
    while i < len(a):
        if a[i] in ("--done", "--settle", "--timeout", "--max-bytes", "--app") and i + 1 < len(a):
            opts[a[i]] = a[i + 1]
            i += 2
        else:
            rest.append(a[i])
            i += 1
    if len(rest) < 3:
        print("usage: brave upload WINDOW SELECTOR FILE... [--done EXPR] [--settle S] [--timeout S] "
              "[--max-bytes N] [--app NAME]", file=sys.stderr)
        return 2
    try:
        out = upload(rest[0], rest[1], rest[2:], app=opts.get("--app", APP), done=opts.get("--done"),
                     settle=float(opts.get("--settle", 4)), timeout=float(opts.get("--timeout", 120)),
                     max_bytes=int(opts.get("--max-bytes", MAX_UPLOAD)))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    except (TimeoutError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0
