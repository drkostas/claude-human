"""Two fingers on an Android touch screen over adb: a pinch, written as the touch screen's own events.

`adb shell input` has one finger, so it cannot pinch. The adb shell user is usually in the `input`
group and may write to `/dev/input/event*`, so a gesture written there as multi-touch events (the
kernel's protocol B, a slot and a tracking id for each finger) goes through Android's own touch
handling and into the app exactly as two real fingers do. The page or app under test sees ordinary
touch events, and the system's own gestures (a browser's zoom, for one) react to them as well.

`checked_pinch` waits for the same conditions as `android.checked_tap` before it touches anything:
the app has focus, no banner shows, no call rings or runs, nobody has touched the phone for `idle`
seconds and the screen around the point holds still. It refuses with the reason when they do not
hold within `wait`, and on a locked phone or a turned screen.

The fingers go down in the two highest slots with tracking ids no real touch uses yet, move one
frame about every 16 ms, and always lift at the end: the last frame lifts both, the script on the
phone writes it again if it is stopped half way, and so does this module when adb fails.

    claude-human android pinch --serial HOST:PORT --package com.example.app X Y --in|--out [--slow]
"""
from __future__ import annotations

import re
import struct
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from claude_human import android

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BTN_TOUCH, BTN_TOOL_FINGER = 0x14A, 0x145
ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR = 0x2F, 0x30
ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x35, 0x36, 0x39
#: tracking ids for the two fingers: a real touch numbers its own from 0 upward
TRACKING_IDS = (61000, 61001)
#: how wide a finger reads, in the device's own touch-size units
FINGER_SIZE = 20
#: the fingers' distance at the narrow end of the gesture, as a share of the wide end
NARROW = 0.3
#: seconds between frames, about one screen refresh
GAP = 0.016

GETEVENT = "getevent -pl"
WM_SIZE = "wm size"
ABI = "getprop ro.product.cpu.abi"
ROTATION = "dumpsys window | grep -m1 -oE 'mRotation=[0-9A-Z_]+'"
REMOTE = "/data/local/tmp/claude-human-pinch"


class Panel(NamedTuple):
    """The touch screen's input device, and the ranges its events use."""
    path: str
    max_x: int
    max_y: int
    max_slot: int
    has_major: bool


def touchscreen(getevent_pl: str) -> Panel | None:
    """Pure. The device `getevent -pl` lists with multi-touch positions and INPUT_PROP_DIRECT (a
    screen, not a touchpad), or None."""
    for block in re.split(r"(?m)^add device \d+: ", getevent_pl or "")[1:]:
        path = block.split()[0] if block.split() else ""
        if "ABS_MT_POSITION_X" not in block or "INPUT_PROP_DIRECT" not in block:
            continue

        def top(code: str) -> int | None:
            m = re.search(rf"{code}\s*:.*?\bmax (\d+)", block)
            return int(m.group(1)) if m else None
        x, y, slot = top("ABS_MT_POSITION_X"), top("ABS_MT_POSITION_Y"), top("ABS_MT_SLOT")
        if x and y and slot:
            return Panel(path, x, y, slot, "ABS_MT_TOUCH_MAJOR" in block)
    return None


def screen_size(wm_size: str) -> tuple[int, int] | None:
    """Pure. The screen's size in pixels from `wm size`; an override size wins over the physical."""
    sizes = dict(re.findall(r"(Physical|Override) size: (\d+x\d+)", wm_size or ""))
    size = sizes.get("Override") or sizes.get("Physical")
    if not size:
        return None
    w, h = size.split("x")
    return int(w), int(h)


def rotation(text: str) -> int | None:
    """Pure. The display's rotation (0 to 3) from `mRotation=ROTATION_1` or `mRotation=1`, or None."""
    m = re.search(r"mRotation=(?:ROTATION_)?(\d)", text or "")
    return int(m.group(1)) if m else None


def event(kind: int, code: int, value: int, *, wide: bool = True) -> bytes:
    """Pure. One `struct input_event`: a time the kernel ignores on write (two longs, 8 bytes each
    on a 64-bit userspace, 4 on a 32-bit one), then type, code and value."""
    return struct.pack("<qqHHi" if wide else "<llHHi", 0, 0, kind, code, value)


def pinch_frames(x: int, y: int, direction: str, *, panel: Panel, screen: tuple[int, int],
                 span: int = 600, steps: int = 12, wide: bool = True) -> list[bytes]:
    """Pure. The frames of a two-finger pinch around (x, y) in screen pixels: fingers that start
    `span` pixels apart and come together to NARROW of that (`in`), or the reverse (`out`). The
    first frame puts both down, the last one lifts both, and every frame ends with SYN_REPORT."""
    if direction not in ("in", "out"):
        raise ValueError(f"a pinch goes in or out, not {direction!r}")
    w, h = screen
    sx, sy = (panel.max_x + 1) / w, (panel.max_y + 1) / h
    slots = (panel.max_slot - 1, panel.max_slot)
    wide_d, narrow_d = span, span * NARROW
    steps = max(2, steps)

    def ev(kind, code, value):
        return event(kind, code, value, wide=wide)

    def at(d: float) -> list[tuple[int, int]]:
        half = d / 2
        pts = [(x - half, y), (x + half, y)]
        return [(min(panel.max_x, max(0, round(px * sx))), min(panel.max_y, max(0, round(py * sy))))
                for px, py in ((min(w - 1, max(0, a)), b) for a, b in pts)]

    frames = []
    for i in range(steps):
        t = i / (steps - 1)
        d = wide_d + (narrow_d - wide_d) * t if direction == "in" else narrow_d + (wide_d - narrow_d) * t
        out = b""
        for slot, tid, (dx, dy) in zip(slots, TRACKING_IDS, at(d)):
            out += ev(EV_ABS, ABS_MT_SLOT, slot)
            if i == 0:
                out += ev(EV_ABS, ABS_MT_TRACKING_ID, tid)
                if panel.has_major:
                    out += ev(EV_ABS, ABS_MT_TOUCH_MAJOR, FINGER_SIZE)
            out += ev(EV_ABS, ABS_MT_POSITION_X, dx) + ev(EV_ABS, ABS_MT_POSITION_Y, dy)
        if i == 0:
            out += ev(EV_KEY, BTN_TOUCH, 1) + ev(EV_KEY, BTN_TOOL_FINGER, 1)
        frames.append(out + ev(EV_SYN, SYN_REPORT, 0))
    frames.append(lift_frame(panel, wide=wide))
    return frames


def lift_frame(panel: Panel, *, wide: bool = True) -> bytes:
    """Pure. Both fingers up."""
    out = b""
    for slot in (panel.max_slot - 1, panel.max_slot):
        out += event(EV_ABS, ABS_MT_SLOT, slot, wide=wide) + event(EV_ABS, ABS_MT_TRACKING_ID, -1, wide=wide)
    return (out + event(EV_KEY, BTN_TOUCH, 0, wide=wide) + event(EV_KEY, BTN_TOOL_FINGER, 0, wide=wide)
            + event(EV_SYN, SYN_REPORT, 0, wide=wide))


def play_script(device: str, gap: float) -> str:
    """Pure. The shell script that writes each frame in turn, and the lift frame again on any exit."""
    return (f"cd {REMOTE} || exit 1\n"
            f"trap 'cat lift.ev > {device}' EXIT INT TERM HUP\n"
            f"for f in f*.ev; do cat \"$f\" > {device} || exit 1; sleep {gap}; done\n")


def checked_pinch(serial: str, package: str, x: int, y: int, direction: str, *, span: int = 600,
                  steps: int = 12, slow: bool = False, idle: float = 3.0, still: bool = True,
                  wait: float = 10.0, settle: float = 1.0, adb: str = android.ADB,
                  env: Mapping[str, str] | None = None, run: android.Runner | None = None,
                  sleep=time.sleep, clock=time.monotonic) -> dict:
    """Pinch around (x, y) in `package` on the device `serial` once every check holds.

    {"outcome": "success" | "refused", "why", "focus_before", "focus_after", "device", "frames"}.
    Nothing touches the screen on a refusal. `slow` takes four times the steps."""
    run = run or android._runner(adb, env)
    report: dict = {"outcome": "refused", "why": "", "focus_before": "", "focus_after": "",
                    "device": None, "frames": 0}
    if direction not in ("in", "out"):
        report["why"] = f"a pinch goes in or out, not {direction!r}"
        return report
    if android.locked(serial, run):
        report["why"] = "the phone is locked"
        return report
    panel = touchscreen(android._read(serial, run, GETEVENT) or "")
    if panel is None:
        report["why"] = "no touch screen device could be read (getevent -pl)"
        return report
    screen = screen_size(android._read(serial, run, WM_SIZE) or "")
    if screen is None:
        report["why"] = "the screen size could not be read (wm size)"
        return report
    turned = rotation(android._read(serial, run, ROTATION) or "")
    if turned != 0:
        report["why"] = ("the screen is turned, and a pinch is written in the panel's upright "
                         f"coordinates (rotation {turned if turned is not None else 'unreadable'})")
        return report
    if package not in android.focus(serial, run) and not android.launch(serial, package, run):
        report["why"] = f"{package} has no launcher activity"
        return report
    end = clock() + wait
    while (why := android.why_not(serial, package, y, run, idle=idle, still=still, sleep=sleep)) is not None:
        if clock() >= end:
            report["why"] = why
            return report
        sleep(0.5)
    wide = "64" in (android._read(serial, run, ABI) or "64")
    frames = pinch_frames(x, y, direction, panel=panel, screen=screen, span=span,
                          steps=steps * (4 if slow else 1), wide=wide)
    report.update(device=panel.path, frames=len(frames))
    with tempfile.TemporaryDirectory(prefix="claude-human-pinch-") as tmp:
        folder = Path(tmp)
        for i, f in enumerate(frames[:-1]):
            (folder / f"f{i:03d}.ev").write_bytes(f)
        (folder / "lift.ev").write_bytes(frames[-1])
        (folder / "play.sh").write_text(play_script(panel.path, GAP))
        run(["-s", serial, "shell", f"rm -rf {REMOTE} && mkdir -p {REMOTE}"])
        code, said = run(["-s", serial, "push", f"{folder}/.", REMOTE])
        if code != 0:
            report["why"] = f"adb could not copy the gesture to the phone: {said.strip()[:200]}"
            return report
    # the last check right before the fingers go down: focus, banners and calls
    if (why := android.why_not(serial, package, y, run, idle=0, still=False, sleep=sleep)) is not None:
        report["why"] = why
        return report
    report["focus_before"] = android.focus(serial, run)
    code, said = run(["-s", serial, "shell", f"sh {REMOTE}/play.sh"])
    if code != 0:
        run(["-s", serial, "shell", f"cat {REMOTE}/lift.ev > {panel.path}"])
        report["why"] = f"the gesture did not finish, and both fingers were lifted: {said.strip()[:200]}"
        return report
    report.update(outcome="success", why=f"pinched {direction} around ({int(x)}, {int(y)}) in {package}")
    sleep(settle)
    report["focus_after"] = android.focus(serial, run)
    return report


def summary(report: dict) -> str:
    """Pure. One line for a person: what was done or why not."""
    if report["outcome"] != "success":
        return report["why"]
    parts = [report["why"], f"{report['frames']} frames on {report['device']}"]
    if report["focus_after"] and report["focus_after"] != report["focus_before"]:
        parts.append(f"focus is now {report['focus_after']}")
    return "; ".join(parts)
