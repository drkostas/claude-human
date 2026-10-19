"""Tap an Android app only when its own window has focus, over adb.

A tap is sent to whatever window has focus at that moment. Checking which activity is on top is not
enough: a notification shade, a dialog or the lock screen can lie over the app while it is still the
top activity, and the tap then lands on them. The window manager's `mCurrentFocus` names the window
that receives input, so that is what this checks, before every tap.

`tap` refuses on a locked phone, brings the app forward when it is not focused (through its launcher
activity with `am start -n`; never `monkey`, which also injects a random event, and a rotation event
turns auto-rotate back on), waits for its window to take focus, taps, and can save a screenshot.

    claude-human android tap --serial HOST:PORT --package com.example.app X Y [--out shot.png]
"""
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence

ADB = "adb"

#: run(argv) -> (exit code, stdout as text); swapped in tests
Runner = Callable[[Sequence[str]], tuple[int, str]]


def _runner(adb: str, env: Mapping[str, str] | None) -> Runner:
    def run(argv: Sequence[str]) -> tuple[int, str]:
        try:
            r = subprocess.run([adb, *argv], capture_output=True, text=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return r.returncode, r.stdout
    return run


def focus(serial: str, run: Runner) -> str:
    """The window that receives input now (its `mCurrentFocus` line), or ""."""
    _, out = run(["-s", serial, "shell", "dumpsys", "window"])
    return next((line.strip() for line in out.splitlines() if "mCurrentFocus" in line), "")


def locked(serial: str, run: Runner) -> bool:
    _, out = run(["-s", serial, "shell", "dumpsys", "window"])
    return "isKeyguardShowing=true" in out


def launch(serial: str, package: str, run: Runner) -> bool:
    """Start the app's launcher activity with `am start -n`. False when it has none."""
    _, out = run(["-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief",
                  "-c", "android.intent.category.LAUNCHER", package])
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines or "/" not in lines[-1]:
        return False
    code, _ = run(["-s", serial, "shell", "am", "start", "-n", lines[-1]])
    return code == 0


def tap(serial: str, package: str, x: int, y: int, *, out: str | None = None, wait: float = 10.0,
        settle: float = 0.0, adb: str = ADB, env: Mapping[str, str] | None = None,
        run: Runner | None = None, sleep=time.sleep) -> tuple[str, str]:
    """Tap (x, y) in `package` on the device `serial`, only once its window has focus.

    Returns ("success", what was done) or ("refused", why): a locked phone, an app with no launcher
    activity, or a window that did not take focus within `wait` seconds. Nothing is tapped on a
    refusal. With `out`, a screenshot is saved `settle` seconds after the tap."""
    run = run or _runner(adb, env)
    if locked(serial, run):
        return "refused", "the phone is locked"
    if package not in focus(serial, run) and not launch(serial, package, run):
        return "refused", f"{package} has no launcher activity"
    end = time.monotonic() + wait
    while package not in (now := focus(serial, run)):
        if time.monotonic() >= end:
            return "refused", f"{package} does not have focus ({now or 'nothing focused'})"
        sleep(0.5)
    code, said = run(["-s", serial, "shell", "input", "tap", str(int(x)), str(int(y))])
    if code != 0:
        return "refused", f"adb could not tap: {said.strip()[:200]}"
    done = f"tapped ({int(x)}, {int(y)}) in {package}"
    if out:
        sleep(settle)
        try:
            shot = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"],
                                  capture_output=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return "success", done + "; no screenshot"
        if shot.returncode == 0 and shot.stdout:
            with open(out, "wb") as f:
                f.write(shot.stdout)
            done += f"; screenshot {out}"
    return "success", done


def main(argv: list[str]) -> int:
    import argparse   # noqa: PLC0415
    p = argparse.ArgumentParser(prog="claude-human android")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tap", help="tap an app only once its own window has focus")
    t.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    t.add_argument("--package", required=True, help="the app's package name")
    t.add_argument("x", type=int)
    t.add_argument("y", type=int)
    t.add_argument("--out", help="save a screenshot here after the tap")
    t.add_argument("--settle", type=float, default=4.0, help="seconds between the tap and the screenshot")
    t.add_argument("--adb", default=ADB)
    a = p.parse_args(argv)
    outcome, said = tap(a.serial, a.package, a.x, a.y, out=a.out, settle=a.settle, adb=a.adb)
    print(said if outcome == "success" else f"REFUSED: {said}")
    return 0 if outcome == "success" else 2
