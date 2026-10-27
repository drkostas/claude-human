"""Tap an Android app over adb only when the tap can land where it is meant to, and say what it did.

A tap is sent to whatever is under the finger at that moment. Checking which activity is on top is
not enough: a notification shade, a dialog or the lock screen can lie over the app while it is still
the top activity, and the tap then lands on them. The window manager's `mCurrentFocus` names the
window that receives input, so that is checked before every tap. It is not enough either, so the
tap also waits for all of these, and refuses with the reason when they do not hold within `wait`:

  - no heads-up notification. A banner takes no focus, so `mCurrentFocus` still names the app while
    one slides in at the top of the screen, and a tap there opens the notification instead.
    SystemUI's own state says when one is pinned or still leaving (`heads_up`).
  - no call, by the telephony registry's call state or the audio mode (`in_call`), so a tap never
    lands on a call screen or interrupts one.
  - no input for `idle` seconds (`dumpsys power`'s last user activity, `idle_s`), so a tap does not
    race the person using the phone. A tap is input too, so taps in a row are `idle` seconds apart.
  - a still screen around the point: a band of rows around it hashes the same twice (`band_hash`),
    hashed on the phone so only the hash travels. A drawer or a page still animating takes a tap
    meant for what comes after it.

`tap` refuses on a locked phone and brings the app forward when it is not focused (launch_how: its
launcher activity with `am start -n`, the launcher intent, then a link the app opens; never `monkey`,
which also injects a random event, and a rotation event turns auto-rotate back on). After the tap it reads the screen back with
`uiautomator dump` and reports what changed: the focused window, and the texts that appeared and
went. It can also save a screenshot.

    claude-human android tap --serial HOST:PORT --package com.example.app X Y [--json] [--out shot.png]

Reading the app's screen is `android read` (checked_read): the view tree's nodes, read only while the
app's own window has focus and the phone is unlocked.

    claude-human android read --serial HOST:PORT --package com.example.app [--json] [--no-start]

Two fingers (a pinch) are `android pinch`, in android_touch, behind the same checks. Reading a page
in the phone's Chromium browser is `android browser`, in android_browser.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence

ADB = "adb"

#: run(argv) -> (exit code, stdout as text); swapped in tests
Runner = Callable[[Sequence[str]], tuple[int, str]]

#: SystemUI's lines about heads-up notifications, filtered on the phone so only these travel
SYSTEMUI = ("dumpsys activity service com.android.systemui/.SystemUIService | grep -E "
            "'mHeadsUpPinnedMode|mHeadsUpAnimatingAway|isHeadsUpState|isPinned|HeadsUpManager state|mTouchableRegion='")
#: the call state of each SIM (0 idle, 1 ringing, 2 off the hook)
TELEPHONY = "dumpsys telephony.registry | grep mCallState="
#: the audio mode, which also shows a call an app makes over the internet
AUDIO = "dumpsys audio | grep -E 'Actual mode|mMode='"
#: audio modes that mean a call is ringing or in progress
CALL_MODES = ("MODE_RINGTONE", "MODE_IN_CALL", "MODE_IN_COMMUNICATION", "MODE_CALL_SCREENING",
              "MODE_CALL_REDIRECT", "MODE_COMMUNICATION_REDIRECT")
#: the last user activity on the phone's uptime clock, and lines that give that clock's now
POWER = "dumpsys power | grep -E 'mLastUserActivityTime|ms ago'"
#: the size and format of a raw screen capture: its first four 32-bit words, then its length
FRAME = "screencap | head -c 16 | od -An -tu4; screencap | wc -c"
#: the view tree, as uiautomator dumps it (it waits for the app to be idle, and fails when it is not)
DUMP = ("uiautomator dump /sdcard/claude-human-tap.xml >/dev/null 2>&1 && cat /sdcard/claude-human-tap.xml; "
        "rm -f /sdcard/claude-human-tap.xml")
#: bytes per pixel of the raw capture formats (RGBA_8888, RGBX_8888, RGB_888, RGB_565, BGRA_8888)
BYTES = {1: 4, 2: 4, 3: 3, 4: 2, 5: 4}
#: rows above and below the tap point that must keep still, and the time between the two hashes
BAND, STILL_GAP = 120, 0.4
#: how many texts that appeared or went a report names, and how much of each
REPORTED, TEXT_MAX = 10, 60
#: links that open an app when neither its launcher activity nor the launcher intent starts it from adb
LINKS = {"com.viber.voip": "viber://chats"}


def _runner(adb: str, env: Mapping[str, str] | None) -> Runner:
    def run(argv: Sequence[str]) -> tuple[int, str]:
        try:
            r = subprocess.run([adb, *argv], capture_output=True, text=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return r.returncode, r.stdout
    return run


def _read(serial: str, run: Runner, command: str) -> str | None:
    """One shell command's output, or None when adb failed or it printed nothing."""
    code, out = run(["-s", serial, "shell", command])
    return out if code == 0 and out.strip() else None


def focus(serial: str, run: Runner) -> str:
    """The window that receives input now (its `mCurrentFocus` line), or ""."""
    _, out = run(["-s", serial, "shell", "dumpsys", "window"])
    return next((line.strip() for line in out.splitlines() if "mCurrentFocus" in line), "")


def locked(serial: str, run: Runner) -> bool:
    _, out = run(["-s", serial, "shell", "dumpsys", "window"])
    return "isKeyguardShowing=true" in out


def started(code: int, said: str) -> bool:
    """Pure. Whether `am start` started something. It can exit 0 and still print "Error: Activity
    class ... does not exist" or "Error: Activity not started, unable to resolve Intent"; a warning
    that the app was already in front ("intent has been delivered") is a start."""
    return code == 0 and not any(line.strip().startswith("Error") for line in said.splitlines())


def launch_how(serial: str, package: str, run: Runner, *, link: str | None = None) -> str | None:
    """Start the app, and say which way worked, or None when none did.

    In turn: its launcher activity by name (`am start -n`), the launcher intent limited to the
    package, and a link the app opens (`link`, or the one LINKS names for the package), also
    limited to the package so no other app takes it. Some phones name a launcher activity that
    `am start -n` then reports as missing (an activity alias the app switches between)."""
    _, out = run(["-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief",
                  "-c", "android.intent.category.LAUNCHER", package])
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    ways: list[tuple[str, list[str]]] = []
    if lines and "/" in lines[-1]:
        ways.append((f"activity {lines[-1]}", ["am", "start", "-n", lines[-1]]))
    ways.append(("launcher intent", ["am", "start", "-a", "android.intent.action.MAIN",
                                     "-c", "android.intent.category.LAUNCHER", "-p", package]))
    if uri := link or LINKS.get(package):
        ways.append((f"link {uri}", ["am", "start", "-a", "android.intent.action.VIEW", "-d", uri,
                                     "-p", package]))
    for how, argv in ways:
        if started(*run(["-s", serial, "shell", *argv])):
            return how
    return None


def launch(serial: str, package: str, run: Runner, *, link: str | None = None) -> bool:
    """Start the app (launch_how). False when no way started it."""
    return launch_how(serial, package, run, link=link) is not None


def heads_up(systemui: str) -> str | None:
    """Pure. Why a heads-up notification may be on the screen, read from SYSTEMUI's lines, or None.

    Read on Android 16: the notification panel prints `mHeadsUpPinnedMode` and
    `mHeadsUpAnimatingAway`, a notification row `isHeadsUpState()` and `isPinned()`, and the first
    `mTouchableRegion` after "HeadsUpManager state:" is the banner's area, empty when none shows."""
    for line in systemui.splitlines():
        s = line.strip()
        if s.startswith("mHeadsUpPinnedMode=true"):
            return "a heads-up notification is pinned on the screen"
        if s.startswith("mHeadsUpAnimatingAway=true"):
            return "a heads-up notification is still leaving the screen"
        if "isHeadsUpState()=true" in s or "isPinned()=true" in s or re.search(r"\bisPinned=true", s):
            return "a notification shows as a heads-up"
    _, _, after = systemui.partition("HeadsUpManager state:")
    region = re.search(r"mTouchableRegion=SkRegion\((.*)\)\s*$", after, re.M)
    if region and region.group(1).strip():
        return "a heads-up notification takes touches on the screen"
    return None


def in_call(telephony: str, audio: str) -> str | None:
    """Pure. Why a call may be ringing or in progress, from the call states and the audio mode, or None."""
    busy = [int(s) for s in re.findall(r"mCallState=(\d+)", telephony) if int(s) != 0]
    if busy:
        return f"a call is ringing or in progress (call state {busy[0]})"
    mode = re.search(r"Actual mode = (MODE_\w+)", audio) or re.search(r"mMode=(MODE_\w+)", audio)
    if mode and mode.group(1) in CALL_MODES:
        return f"a call is ringing or in progress ({mode.group(1)})"
    return None


def idle_s(power: str) -> float | None:
    """Pure. Seconds since the last input on the phone, or None when it cannot be told.

    `mLastUserActivityTime` is a time on the phone's uptime clock, and any line of the form
    "name=<time> (<n> ms ago)" gives that clock's now (the time plus n)."""
    last = [int(v) for v in re.findall(r"mLastUserActivityTime(?:\([^)]*\))?=(\d+)", power)]
    pair = re.search(r"=(\d+) \((\d+) ms ago\)", power)
    if not last or not pair:
        return None
    now = int(pair.group(1)) + int(pair.group(2))
    return max(0.0, (now - max(last)) / 1000.0)


def frame(serial: str, run: Runner) -> tuple[int, int, int, int] | None:
    """(width, height, bytes per pixel, header bytes) of a raw screen capture, or None."""
    out = _read(serial, run, FRAME)
    nums = [int(n) for n in (out or "").split() if n.isdigit()]
    if len(nums) < 5:
        return None
    width, height, fmt, total = nums[0], nums[1], nums[2], nums[-1]
    bpp = BYTES.get(fmt, 4)
    header = total - width * height * bpp
    return (width, height, bpp, header) if width and height and 0 <= header <= 64 else None


def band_hash(serial: str, run: Runner, shape: tuple[int, int, int, int], y: int, half: int = BAND) -> str | None:
    """The md5 of the rows from y - half to y + half of a raw capture, hashed on the phone, or None."""
    width, height, bpp, header = shape
    top, bottom = max(0, y - half), min(height, y + half)
    start = header + top * width * bpp + 1
    out = _read(serial, run, f"screencap | tail -c +{start} | head -c {(bottom - top) * width * bpp} | md5sum")
    found = re.match(r"([0-9a-f]{32})", (out or "").strip())
    return found.group(1) if found else None


def texts(xml: str | None) -> list[str] | None:
    """Pure. The texts and content descriptions of a uiautomator dump, in order, once each; None
    when it is not a dump (uiautomator fails while the app keeps animating)."""
    if not xml or "<hierarchy" not in xml:
        return None
    out: list[str] = []
    for m in re.finditer(r'\b(?:text|content-desc)="([^"]*)"', xml):
        t = html.unescape(m.group(1)).strip()
        if t and t not in out:
            out.append(t)
    return out


def why_not(serial: str, package: str, y: int, run: Runner, *, idle: float = 3.0, still: bool = True,
            sleep=time.sleep) -> str | None:
    """Why a tap at height `y` in `package` is not safe now, or None when it is (each check above)."""
    now = focus(serial, run)
    if package not in now:
        return f"{package} does not have focus ({now or 'nothing focused'})"
    ui = _read(serial, run, SYSTEMUI)
    if ui is None:
        return "SystemUI's heads-up state could not be read"
    if why := heads_up(ui):
        return why
    tel, aud = _read(serial, run, TELEPHONY), _read(serial, run, AUDIO)
    if tel is None and aud is None:
        return "the call state could not be read"
    if why := in_call(tel or "", aud or ""):
        return why
    if idle > 0:
        quiet = idle_s(_read(serial, run, POWER) or "")
        if quiet is None:
            return "the time of the last input could not be read"
        if quiet < idle:
            return f"the phone was used {quiet:.1f} s ago (a tap waits for {idle:g} s without input)"
    if still:
        shape = frame(serial, run)
        first = band_hash(serial, run, shape, y) if shape else None
        sleep(STILL_GAP)
        second = band_hash(serial, run, shape, y) if shape else None
        if first is None or second is None:
            return "the screen around the tap could not be read"
        if first != second:
            return "the screen around the tap is still moving"
    return None


def _short(items: list[str]) -> list[str]:
    return [t if len(t) <= TEXT_MAX else t[:TEXT_MAX - 1] + "…" for t in items[:REPORTED]]


def checked_tap(serial: str, package: str, x: int, y: int, *, idle: float = 3.0, still: bool = True,
                read: bool = True, wait: float = 10.0, settle: float = 1.0, out: str | None = None,
                link: str | None = None, adb: str = ADB, env: Mapping[str, str] | None = None,
                run: Runner | None = None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Tap (x, y) in `package` on the device `serial` once every check holds, and read the screen back.

    {"outcome": "success" | "refused", "why", "focus_before", "focus_after", "appeared", "gone",
    "screenshot", "launched"}. Nothing is tapped on a refusal: a locked phone, an app no way could
    start, or a check that did not hold within `wait` seconds (its reason). An app without focus is
    started first (launch_how, with `link` as its last way), and `launched` says which way worked.
    The view tree is read before the tap and `settle` seconds after it, and the texts that appeared
    and went are named."""
    run = run or _runner(adb, env)
    report: dict = {"outcome": "refused", "why": "", "focus_before": "", "focus_after": "",
                    "appeared": None, "gone": None, "screenshot": None, "launched": None}
    if locked(serial, run):
        report["why"] = "the phone is locked"
        return report
    if package not in focus(serial, run):
        report["launched"] = launch_how(serial, package, run, link=link)
        if report["launched"] is None:
            report["why"] = f"{package} could not be started (no launcher activity, launcher intent or link started it)"
            return report
    end = clock() + wait
    while (why := why_not(serial, package, y, run, idle=idle, still=still, sleep=sleep)) is not None:
        if clock() >= end:
            report["why"] = why
            return report
        sleep(0.5)
    before = texts(_read(serial, run, DUMP)) if read else None
    # the dump takes a couple of seconds: focus, banners and calls once more, right before the tap
    if read and (why := why_not(serial, package, y, run, idle=0, still=False, sleep=sleep)) is not None:
        report["why"] = why
        return report
    report["focus_before"] = focus(serial, run)
    code, said = run(["-s", serial, "shell", "input", "tap", str(int(x)), str(int(y))])
    if code != 0:
        report["why"] = f"adb could not tap: {said.strip()[:200]}"
        return report
    report.update(outcome="success", why=f"tapped ({int(x)}, {int(y)}) in {package}")
    sleep(settle)
    report["focus_after"] = focus(serial, run)
    if read:
        after = texts(_read(serial, run, DUMP))
        if before is not None and after is not None:
            report["appeared"] = _short([t for t in after if t not in before])
            report["gone"] = _short([t for t in before if t not in after])
    if out:
        try:
            shot = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"],
                                  capture_output=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired):
            shot = None
        if shot is not None and shot.returncode == 0 and shot.stdout:
            with open(out, "wb") as f:
                f.write(shot.stdout)
            report["screenshot"] = out
    return report


def summary(report: dict) -> str:
    """Pure. One line for a person: what was done or why not, and what changed on the screen."""
    if report["outcome"] != "success":
        return report["why"]
    parts = [report["why"]]
    if report["focus_after"] and report["focus_after"] != report["focus_before"]:
        parts.append(f"focus is now {report['focus_after']}")
    if report["appeared"] is None:
        parts.append("the screen was not read back")
    elif report["appeared"] or report["gone"]:
        parts.append(f"{len(report['appeared'])} text(s) appeared, {len(report['gone'])} went")
    else:
        parts.append("no text on the screen changed")
    if report["screenshot"]:
        parts.append(f"screenshot {report['screenshot']}")
    return "; ".join(parts)


def tap(serial: str, package: str, x: int, y: int, *, out: str | None = None, wait: float = 10.0,
        settle: float = 1.0, adb: str = ADB, env: Mapping[str, str] | None = None,
        run: Runner | None = None, sleep=time.sleep, **checks) -> tuple[str, str]:
    """checked_tap as (outcome, one line): ("success", what was done and what changed) or
    ("refused", why). `checks` are checked_tap's idle, still, read and clock."""
    report = checked_tap(serial, package, x, y, out=out, wait=wait, settle=settle, adb=adb, env=env,
                         run=run, sleep=sleep, **checks)
    return report["outcome"], summary(report)


def _flag(node: ET.Element, name: str) -> bool:
    return node.get(name) == "true"


def nodes(xml: str | None) -> list[dict] | None:
    """Pure. The nodes of a uiautomator dump that say something or can be pressed, in order: each
    with a text or a content description, or clickable with a resource id. Each is {"text", "desc",
    "id", "class", "package", "bounds": [left, top, right, bottom], "clickable", "scrollable",
    "focused", "selected"}. None when it is not a dump."""
    if not xml or "<hierarchy" not in xml:
        return None
    try:
        root = ET.fromstring(xml[xml.index("<hierarchy"):])
    except ET.ParseError:
        return None
    out: list[dict] = []
    for n in root.iter("node"):
        text, desc, rid = n.get("text", ""), n.get("content-desc", ""), n.get("resource-id", "")
        if not (text.strip() or desc.strip() or (rid and _flag(n, "clickable"))):
            continue
        box = [int(v) for v in re.findall(r"-?\d+", n.get("bounds", ""))][:4]
        out.append({"text": text, "desc": desc, "id": rid, "class": n.get("class", ""),
                    "package": n.get("package", ""), "bounds": box if len(box) == 4 else None,
                    "clickable": _flag(n, "clickable"), "scrollable": _flag(n, "scrollable"),
                    "focused": _flag(n, "focused"), "selected": _flag(n, "selected")})
    return out


def _window(serial: str, run: Runner) -> tuple[str, bool]:
    """(the focused window's line, whether the keyguard shows), from one `dumpsys window`."""
    _, out = run(["-s", serial, "shell", "dumpsys", "window"])
    now = next((line.strip() for line in out.splitlines() if "mCurrentFocus" in line), "")
    return now, "isKeyguardShowing=true" in out


def checked_read(serial: str, package: str, *, start: bool = True, link: str | None = None,
                 wait: float = 10.0, adb: str = ADB, env: Mapping[str, str] | None = None,
                 run: Runner | None = None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Read what `package` shows on the device `serial`, only while its own window has focus.

    {"outcome": "success" | "refused", "why", "focus", "launched", "nodes", "left_out", "xml"}.
    Refused, with nothing read, on a locked phone (the keyguard showing), when another window has
    focus (a shade, a dialog, another app) for all of `wait` seconds, and when the focus or the lock
    changed while the view tree was read, since the tree may then be another window's. With `start`
    an app without focus is started first (launch_how, `launched` says how); without it, it is
    refused. `nodes` are the app's own (nodes); `left_out` counts the nodes of other packages the
    dump held. `xml` is the dump as read."""
    run = run or _runner(adb, env)
    report: dict = {"outcome": "refused", "why": "", "focus": "", "launched": None, "nodes": None,
                    "left_out": 0, "xml": None}
    now, shut = _window(serial, run)
    if shut:
        report["why"] = "the phone is locked"
        return report
    if package not in now:
        if not start:
            report["why"] = f"{package} does not have focus ({now or 'nothing focused'})"
            return report
        report["launched"] = launch_how(serial, package, run, link=link)
        if report["launched"] is None:
            report["why"] = f"{package} could not be started (no launcher activity, launcher intent or link started it)"
            return report
    end = clock() + wait
    while True:
        now, shut = _window(serial, run)
        if shut:
            report["why"] = "the phone is locked"
            return report
        found = None
        if package in now:
            found = nodes(xml := _read(serial, run, DUMP))
            after, shut = _window(serial, run)
            if shut or after != now:
                report["why"] = ("the phone locked while the screen was read" if shut else
                                 f"the focus moved to {after or 'nothing'} while the screen was read")
                return report
            if found is not None:
                break
        if clock() >= end:
            report["focus"] = now
            report["why"] = (f"{package} does not have focus ({now or 'nothing focused'})" if package not in now
                             else "the view tree could not be read (uiautomator dump failed)")
            return report
        sleep(0.5)
    own = [n for n in found if n["package"] in ("", package)]
    if found and not own:
        others = sorted({n["package"] for n in found})
        report.update(focus=now, why=f"the view tree held only other apps' nodes ({', '.join(others)})")
        return report
    report.update(outcome="success", focus=now, nodes=own, left_out=len(found) - len(own), xml=xml,
                  why=f"read {len(own)} node(s) of {package}")
    return report


def main(argv: list[str]) -> int:
    import argparse   # noqa: PLC0415
    if argv[:1] == ["files"]:
        from claude_human import android_files   # noqa: PLC0415
        return android_files.main(argv[1:])
    if argv[:1] == ["browser"]:
        from claude_human import android_browser   # noqa: PLC0415
        return android_browser.main(argv[1:])
    p = argparse.ArgumentParser(prog="claude-human android")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tap", help="tap an app only once its window has focus, no banner or call is "
                                   "showing, the phone is idle and the screen around the point is still")
    t.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    t.add_argument("--package", required=True, help="the app's package name")
    t.add_argument("x", type=int)
    t.add_argument("y", type=int)
    t.add_argument("--idle", type=float, default=3.0, help="seconds without input before a tap (0: do not wait)")
    t.add_argument("--wait", type=float, default=10.0, help="seconds to wait for every check before refusing")
    t.add_argument("--no-still", action="store_true", help="do not wait for the screen around the point to keep still")
    t.add_argument("--no-read", action="store_true", help="do not read the screen back before and after")
    t.add_argument("--settle", type=float, default=1.0, help="seconds between the tap and reading the screen back")
    t.add_argument("--out", help="save a screenshot here after the tap")
    t.add_argument("--link", help="a link the app opens, tried when it has to be started and its "
                                  "launcher activity and the launcher intent do not start it")
    t.add_argument("--json", action="store_true", help="print the whole report as JSON")
    t.add_argument("--adb", default=ADB)
    r = sub.add_parser("read", help="the texts, bounds and clickable flags on an app's screen, read only "
                                    "while its own window has focus and the phone is unlocked")
    r.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    r.add_argument("--package", required=True, help="the app's package name")
    r.add_argument("--no-start", action="store_true", help="refuse when the app is not in front, do not start it")
    r.add_argument("--link", help="a link the app opens, tried when it has to be started")
    r.add_argument("--wait", type=float, default=10.0, help="seconds to wait for the app's focus and a readable tree")
    r.add_argument("--xml", help="save the view tree as read here")
    r.add_argument("--json", action="store_true", help="print the whole report as JSON (without the tree)")
    r.add_argument("--adb", default=ADB)
    s = sub.add_parser("launch", help="start an app, by its launcher activity, the launcher intent or a "
                                      "link, and say which way worked")
    s.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    s.add_argument("--package", required=True, help="the app's package name")
    s.add_argument("--link", help="a link the app opens, tried last")
    s.add_argument("--adb", default=ADB)
    g = sub.add_parser("pinch", help="pinch with two fingers around a point, behind the same checks as tap")
    g.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    g.add_argument("--package", required=True, help="the app's package name")
    g.add_argument("x", type=int)
    g.add_argument("y", type=int)
    way = g.add_mutually_exclusive_group(required=True)
    way.add_argument("--in", dest="direction", action="store_const", const="in", help="fingers together")
    way.add_argument("--out", dest="direction", action="store_const", const="out", help="fingers apart")
    g.add_argument("--span", type=int, default=600, help="the fingers' distance at the wide end, in pixels")
    g.add_argument("--steps", type=int, default=12, help="frames of movement, about 16 ms each")
    g.add_argument("--slow", action="store_true", help="four times the frames")
    g.add_argument("--idle", type=float, default=3.0, help="seconds without input before the pinch (0: do not wait)")
    g.add_argument("--wait", type=float, default=10.0, help="seconds to wait for every check before refusing")
    g.add_argument("--no-still", action="store_true", help="do not wait for the screen around the point to keep still")
    g.add_argument("--settle", type=float, default=1.0, help="seconds after the pinch before reading the focus")
    g.add_argument("--json", action="store_true", help="print the whole report as JSON")
    g.add_argument("--adb", default=ADB)
    a = p.parse_args(argv)
    if a.cmd == "pinch":
        from claude_human import android_touch   # noqa: PLC0415
        report = android_touch.checked_pinch(a.serial, a.package, a.x, a.y, a.direction, span=a.span,
                                             steps=a.steps, slow=a.slow, idle=a.idle, still=not a.no_still,
                                             wait=a.wait, settle=a.settle, adb=a.adb)
        if a.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            print(android_touch.summary(report) if report["outcome"] == "success" else f"REFUSED: {report['why']}")
        return 0 if report["outcome"] == "success" else 2
    if a.cmd == "launch":
        how = launch_how(a.serial, a.package, _runner(a.adb, None), link=a.link)
        print(f"started {a.package} by its {how}" if how else f"REFUSED: nothing started {a.package}")
        return 0 if how else 2
    if a.cmd == "read":
        report = checked_read(a.serial, a.package, start=not a.no_start, link=a.link, wait=a.wait, adb=a.adb)
        if a.xml and report["xml"]:
            with open(a.xml, "w", encoding="utf-8") as f:
                f.write(report["xml"])
        if a.json:
            print(json.dumps({k: v for k, v in report.items() if k != "xml"}, ensure_ascii=False, indent=1))
        elif report["outcome"] != "success":
            print(f"REFUSED: {report['why']}")
        else:
            for n in report["nodes"]:
                flag = "\tclickable" if n["clickable"] else ""
                print(f"{n['text'] or n['desc'] or n['id']}\t{n['bounds']}{flag}")
        return 0 if report["outcome"] == "success" else 2
    report = checked_tap(a.serial, a.package, a.x, a.y, idle=a.idle, still=not a.no_still,
                         read=not a.no_read, wait=a.wait, settle=a.settle, out=a.out, link=a.link, adb=a.adb)
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(summary(report) if report["outcome"] == "success" else f"REFUSED: {report['why']}")
    return 0 if report["outcome"] == "success" else 2
