"""Press key combinations in one named window of a Mac, through a running station, once the person at
the Mac has stopped using it.

Posting a key event from a process needs macOS's Accessibility grant, and the station already holds
it, so this asks the station (``POST /input`` with ``{"type": "key", ...}``) instead of posting
events itself. The station refuses a key for a named window that is not in front, so a key never
lands in another window.

``press`` waits until nobody has touched the keyboard, mouse or trackpad for ``idle`` seconds (the
HID system's idle time), brings the window forward with yabai when another one is in front, presses
each combination in turn, and puts the window that was in front back afterwards. Only the first press
waits for an idle Mac: the station's own key events count as input, so the idle time is near zero
right after one.

    from claude_human import keys
    keys.parse("cmd+ctrl+left")            # (123, 0x140000): macOS's key code and modifier flags
    keys.press(1234, ["cmd+ctrl+left"], token=token)

    claude-human key --window 1234 cmd+ctrl+left [cmd+ctrl+right ...] [--idle 120] [--wait 3600] [--json]

Key names follow a US (ANSI) keyboard: letters, digits, the arrows, return, tab, space, delete,
escape, home, end, page up and down, the F keys and the common punctuation. The modifiers are cmd,
ctrl, alt (or opt), shift and fn.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping

from .desktops import YABAI

#: the station's address on this Mac (it binds the loopback interface only)
STATION = "http://127.0.0.1:8789"

#: macOS virtual key codes on a US (ANSI) keyboard, by name
CODES: dict[str, int] = {
    **dict(zip("asdfhgzxcv", range(10))), "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16,
    "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "equal": 24, "9": 25, "7": 26,
    "minus": 27, "8": 28, "0": 29, "rightbracket": 30, "o": 31, "u": 32, "leftbracket": 33, "i": 34,
    "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "quote": 39, "k": 40, "semicolon": 41,
    "backslash": 42, "comma": 43, "slash": 44, "n": 45, "m": 46, "period": 47, "tab": 48, "space": 49,
    "grave": 50, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119,
    "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
}

#: the modifier flags a key event carries, by name
MODIFIERS: dict[str, int] = {
    "cmd": 0x100000, "command": 0x100000, "ctrl": 0x40000, "control": 0x40000,
    "alt": 0x80000, "opt": 0x80000, "option": 0x80000, "shift": 0x20000, "fn": 0x800000,
}

_IDLE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def parse(combo: str) -> tuple[int, int]:
    """("cmd+ctrl+left") -> (key code, modifier flags). The key comes last. A name it does not know,
    a modifier given twice or a combination without a key raises ValueError naming the part."""
    parts = [p.strip().lower() for p in (combo or "").split("+")]
    if not parts or not parts[-1]:
        raise ValueError(f"no key in {combo!r}")
    *mods, key = parts
    if key not in CODES:
        raise ValueError(f"unknown key {key!r} in {combo!r}")
    flags = 0
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"unknown modifier {m!r} in {combo!r}")
        if flags & MODIFIERS[m]:
            raise ValueError(f"modifier {m!r} given twice in {combo!r}")
        flags |= MODIFIERS[m]
    return CODES[key], flags


def idle_s(ioreg: str | None) -> float | None:
    """Seconds since the last keyboard, mouse or trackpad input, from ``ioreg -c IOHIDSystem``'s
    output, or None when it holds no idle time."""
    m = _IDLE.search(ioreg or "")
    return int(m.group(1)) / 1e9 if m else None


def idle_seconds(*, run: Callable = subprocess.run, env: Mapping[str, str] | None = None) -> float | None:
    """Seconds since the person last used the keyboard, mouse or trackpad, or None when macOS did
    not say."""
    try:
        r = run(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True,
                timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return idle_s(r.stdout) if r.returncode == 0 else None


def front(*, run: Callable = subprocess.run, yabai: str = YABAI,
          env: Mapping[str, str] | None = None) -> dict | None:
    """The window in front, as yabai describes it ({"id", "app", "title", ...}), or None."""
    try:
        r = run([yabai, "-m", "query", "--windows", "--window"], capture_output=True, text=True,
                timeout=10, env=env)
        got = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("id") else None


def focus(window: int, *, run: Callable = subprocess.run, yabai: str = YABAI,
          env: Mapping[str, str] | None = None) -> bool:
    """Bring `window` forward with yabai. Whether yabai said it did; read `front` to know."""
    try:
        r = run([yabai, "-m", "window", str(int(window)), "--focus"], capture_output=True, text=True,
                timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def post(window: int, code: int, flags: int, *, token: str, station: str = STATION,
         opener: Callable = urllib.request.urlopen) -> dict:
    """One key press for `window`, through the station. The station's answer ({"ok": True}, or
    {"ok": False, "detail"} when the window is not in front, the screen is locked or a dialog is
    up), or {"ok": False, "detail"} when the station could not be asked."""
    req = urllib.request.Request(
        f"{station.rstrip('/')}/input", method="POST",
        data=json.dumps({"type": "key", "code": int(code), "flags": int(flags), "window": int(window)}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with opener(req, timeout=10) as r:
            got = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "detail": f"the station answered {e.code}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "detail": f"the station did not answer: {type(e).__name__}"}
    return got if isinstance(got, dict) else {"ok": False, "detail": "the station's answer was not an object"}


def press(window: int, combos: Iterable[str], *, token: str, station: str = STATION,
          idle: float = 120.0, wait: float = 3600.0, gap: float = 0.5, bring: bool = True,
          restore: bool = True, run: Callable = subprocess.run, opener: Callable = urllib.request.urlopen,
          sleep: Callable = time.sleep, clock: Callable = time.monotonic, yabai: str = YABAI,
          env: Mapping[str, str] | None = None) -> dict:
    """Press each combination in `window`, in order, once the Mac has been idle for `idle` seconds
    (waiting up to `wait`). Returns a report: {"ok", "pressed" (the combinations the station
    pressed), "why" (the first refusal, or None), "front_before" ({"id", "app"} of the window that
    was in front), "brought" (whether the window was brought forward), "restored", "waited_s"}.

    It stops at the first combination the station refuses, and with `bring` False it refuses
    rather than bring the window forward. Every combination is parsed before anything waits, so a
    name it does not know raises ValueError at once."""
    keys = [(c, *parse(c)) for c in combos]
    if not keys:
        raise ValueError("no key combination to press")
    report: dict = {"ok": False, "pressed": [], "why": None, "front_before": None, "brought": False,
                    "restored": False, "waited_s": 0.0}
    start = clock()
    while idle > 0:
        now = idle_seconds(run=run, env=env)
        if now is None:
            report["why"] = "macOS did not say how long the Mac has been idle"
            return report
        if now >= idle:
            break
        if clock() - start >= wait:
            report["why"] = f"the Mac was in use for {wait:g} s"
            report["waited_s"] = round(clock() - start, 1)
            return report
        sleep(min(5.0, max(0.5, idle - now)))
    report["waited_s"] = round(clock() - start, 1)
    before = front(run=run, yabai=yabai, env=env)
    if before is None:
        report["why"] = "yabai did not say which window is in front"
        return report
    report["front_before"] = {"id": before.get("id"), "app": before.get("app")}
    if int(before["id"]) != int(window):
        if not bring:
            report["why"] = f"window {window} is not in front"
            return report
        focus(window, run=run, yabai=yabai, env=env)
        sleep(0.6)
        now_front = front(run=run, yabai=yabai, env=env)
        report["brought"] = True
        if not now_front or int(now_front.get("id") or 0) != int(window):
            report["why"] = f"window {window} did not come to the front"
            _put_back(report, before, restore, run=run, yabai=yabai, env=env)
            return report
    for i, (combo, code, flags) in enumerate(keys):
        if i:
            sleep(gap)
        got = post(window, code, flags, token=token, station=station, opener=opener)
        if not got.get("ok"):
            report["why"] = f"{combo}: {got.get('detail') or 'the station refused it'}"
            break
        report["pressed"].append(combo)
    report["ok"] = report["why"] is None
    _put_back(report, before, restore, run=run, yabai=yabai, env=env)
    return report


def _put_back(report: dict, before: dict, restore: bool, *, run, yabai, env) -> None:
    if report["brought"] and restore and before.get("id"):
        report["restored"] = focus(int(before["id"]), run=run, yabai=yabai, env=env)


def _token(token_file: str | None, environ: Mapping[str, str]) -> str:
    """The station's token: the file named, else $CLAUDE_HUMAN_STATION_TOKEN, else the station's own
    token file. Never printed."""
    from .station.auth import ENV_TOKEN, default_token_file   # noqa: PLC0415
    if not token_file and environ.get(ENV_TOKEN):
        return environ[ENV_TOKEN]
    try:
        with open(token_file or default_token_file()) as f:
            return f.read().strip()
    except OSError:
        return ""


def main(argv: list[str], *, environ: Mapping[str, str] | None = None) -> int:
    import argparse   # noqa: PLC0415
    import os   # noqa: PLC0415
    p = argparse.ArgumentParser(prog="claude-human key", description="press key combinations in one "
                                "window through the station, once the Mac has been idle")
    p.add_argument("combos", nargs="+", help="such as cmd+ctrl+left, return or shift+tab")
    p.add_argument("--window", type=int, required=True, help="the window's id (yabai's, which is macOS's)")
    p.add_argument("--station", default=STATION)
    p.add_argument("--token-file", help="the station's token (default: $CLAUDE_HUMAN_STATION_TOKEN, "
                                        "then the station's own token file)")
    p.add_argument("--idle", type=float, default=120.0, help="seconds without input before the first press (0: do not wait)")
    p.add_argument("--wait", type=float, default=3600.0, help="seconds to wait for that before refusing")
    p.add_argument("--no-bring", action="store_true", help="refuse when the window is not in front instead of bringing it forward")
    p.add_argument("--no-restore", action="store_true", help="leave the window in front afterwards")
    p.add_argument("--json", action="store_true", help="print the whole report as JSON")
    a = p.parse_args(argv)
    token = _token(a.token_file, os.environ if environ is None else environ)
    if not token:
        print("refused: no station token (pass --token-file, or set CLAUDE_HUMAN_STATION_TOKEN)")
        return 2
    try:
        report = press(a.window, a.combos, token=token, station=a.station, idle=a.idle, wait=a.wait,
                       bring=not a.no_bring, restore=not a.no_restore)
    except ValueError as e:
        print(f"refused: {e}")
        return 2
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(("pressed " + ", ".join(report["pressed"])) if report["ok"]
              else f"refused: {report['why']} (pressed {', '.join(report['pressed']) or 'nothing'})")
    return 0 if report["ok"] else 1
