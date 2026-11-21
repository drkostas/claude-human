"""keep_active.py — keep a Mac looking active during a daily window, with a tap that changes nothing.

Some programs (chat and presence apps) turn a person "away" when the Mac reports no keyboard or mouse input
for ten minutes. They ask macOS for ``CGEventSourceSecondsSinceLastEventType(CombinedSessionState,
AnyInputEventType)``. An event posted by a program does not reset that counter. Input from a keyboard does,
and so does input from Karabiner's virtual keyboard, which ``vhid_type`` (built by ``claude-human
build-tools``) uses. A bare tap of the left Shift key (``vhid_type --wake`` with nothing on standard input)
resets the counter, types nothing and has no meaning in any program.

  - ``draw_window`` picks one day's start and end, each within ``spread_min`` minutes either side of the hours
    it is given, so the hours are not the same to the minute every day.
  - ``step`` is one pass of a scheduler, run every minute or so. It draws the day's window once and keeps it
    in a state file, taps when the window is open and the last tap is older than the interval, and says what
    it did. It never taps while another process types through the virtual keyboard (it takes the same lock
    as ``unlock`` and does not wait), so a tap never lands in the middle of typing.
  - The state file holds the drawn window, every tap's time and the person's idle time just before the last
    tap, so a caller can tell its own taps from a person's input, and can tell that a day was missed.
  - ``person_idle`` turns the system's idle reading into the person's own. After a tap the system reads as if
    someone had just used the Mac, so a program that asks "is anyone here" would be told yes. When the last input
    was the tap, the person's idle time is the reading plus what it was before the tap (a lower bound, and exact
    when no one has touched the Mac since). Input after the tap shows as it is.
  - Given an ``idle`` reader, ``step`` taps only when the person's idle time has reached the interval, so a tap is
    never needed while someone is using the Mac, and a tap never hides input from the last interval.

    claude-human keep-active --start 09:00 --end 19:00 [--spread 60] [--interval 240] [--state FILE]
    claude-human keep-active --off          (a day with no window)
    claude-human keep-active --idle         (print the system's idle seconds and exit)
    claude-human keep-active --person-idle  (the same with this module's taps left out)
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import paths, unlock

DEFAULT_STATE = "~/.local/state/claude-human/keep-active.json"
INTERVAL_S = 240
SPREAD_MIN = 60
#: a window shorter than this is not drawn, since the two draws could cross
MIN_WINDOW_S = 1800
#: the most taps one state file keeps, so a long run does not grow it for ever
KEEP_TAPS = 400


class WindowError(ValueError):
    """The hours given cannot make a window."""


def _clock(text: str) -> dt.time:
    try:
        h, m = text.split(":")
        return dt.time(int(h), int(m))
    except (ValueError, TypeError) as e:
        raise WindowError(f"{text!r} is not a time of day written HH:MM") from e


def draw_window(day: dt.date, start: str | dt.time, end: str | dt.time, *, spread_min: int = SPREAD_MIN,
                rng: random.Random | None = None, tz: dt.tzinfo | None = None) -> tuple[dt.datetime, dt.datetime]:
    """One day's real (start, end): each given time moved by a random whole number of minutes between
    -spread_min and +spread_min. The given hours must leave at least half an hour between them, and a draw that
    would cross is moved back so the real window is never shorter than that."""
    rng = rng or random.Random()
    s = _clock(start) if isinstance(start, str) else start
    e = _clock(end) if isinstance(end, str) else end
    base_s = dt.datetime.combine(day, s, tzinfo=tz)
    base_e = dt.datetime.combine(day, e, tzinfo=tz)
    if base_e - base_s < dt.timedelta(seconds=MIN_WINDOW_S):
        raise WindowError(f"{s:%H:%M} to {e:%H:%M} leaves under {MIN_WINDOW_S // 60} minutes")
    got_s = base_s + dt.timedelta(minutes=rng.randint(-spread_min, spread_min))
    got_e = base_e + dt.timedelta(minutes=rng.randint(-spread_min, spread_min))
    if got_e - got_s < dt.timedelta(seconds=MIN_WINDOW_S):
        got_e = got_s + dt.timedelta(seconds=MIN_WINDOW_S)
    return got_s, got_e


def idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard or mouse input, as the system reports them to a program, or None when
    that cannot be read (not macOS, or pyobjc-framework-Quartz is missing)."""
    try:
        import Quartz   # noqa: PLC0415
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType))
    except Exception:   # noqa: BLE001 - any failure to read means "cannot be read"
        return None


#: how close the idle reading must be to the age of the last tap for that tap to be the last input
TAP_SLACK_S = 10.0


def person_idle(raw: Optional[float], state_path: str | os.PathLike, *, now: dt.datetime | None = None,
                slack_s: float = TAP_SLACK_S) -> Optional[float]:
    """The idle seconds of the person, given the system's reading `raw`. When the last input was this module's
    tap (the reading is about as old as the tap, within `slack_s`), it is the reading plus the person's idle time
    just before that tap. Otherwise the reading is the person's, and None stays None."""
    if raw is None:
        return None
    last = _load(Path(state_path).expanduser()).get("last_tap")
    try:
        at = dt.datetime.fromisoformat(last["at"])
        before = float(last["idle_before"])
        age = ((now or dt.datetime.now(at.tzinfo)) - at).total_seconds()
    except (TypeError, KeyError, ValueError):
        return raw
    return raw + before if abs(raw - age) <= slack_s else raw


def tap(*, helper: str | os.PathLike | None = None, sudo: bool = True, wait_s: float = 0.0,
        run: Callable | None = None, env: Mapping[str, str] | None = None,
        lock: unlock.SessionLock | None = None, timeout: float = 30.0) -> tuple[bool, str]:
    """Tap a bare Shift once through the virtual keyboard. Returns (ok, detail). It takes the keyboard lock
    first and, with the default `wait_s` of 0, does not wait for it: when another process is typing the tap is
    skipped and the detail starts with "busy"."""
    lock = lock or unlock.hid_lock()
    held, why = lock.take("keep-active tap", wait_s)
    if held is None:
        return False, f"busy: {why}"
    try:
        argv = unlock.helper_argv(str(helper or paths.vhid_path()), "--wake", sudo=sudo)
        try:
            r = (run or subprocess.run)(argv, input="", text=True, capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return False, "the virtual keyboard helper timed out"
        except OSError as e:
            return False, f"could not run the virtual keyboard helper: {str(e)[:60]}"
        if r.returncode != 0:
            return False, f"the virtual keyboard helper failed: {(r.stderr or r.stdout or '').strip()[:80]}"
        return True, "tapped"
    finally:
        lock.release(held)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def step(now: dt.datetime, *, start: str | dt.time | None, end: str | dt.time | None, state_path: str | os.PathLike,
         spread_min: int = SPREAD_MIN, interval_s: int = INTERVAL_S, rng: random.Random | None = None,
         do_tap: Callable[[], tuple[bool, str]] | None = None,
         idle: Callable[[], Optional[float]] | None = None) -> dict:
    """One pass. `now` carries the zone the hours are in. `start` and `end` are the hours for `now`'s date, or
    both None for a day with no window. Returns {"did": ..., "window": [start, end] or None, ...} where `did` is one
    of "off", "outside", "waiting", "active", "tapped", "busy" or "failed". A tap is recorded in the state file with its
    time, and the window drawn for a date stays the same for every later pass that day, whatever hours are given
    then. With `idle` (a reader of the system's idle seconds, such as `idle_seconds`) a pass taps only when the person's
    idle time has reached `interval_s`, and says "active" otherwise, since the Mac is already in use."""
    path = Path(state_path).expanduser()
    state = _load(path)
    day = now.date().isoformat()
    if start is None or end is None:
        return {"did": "off", "window": None}
    if state.get("day") != day:
        s, e = draw_window(now.date(), start, end, spread_min=spread_min, rng=rng, tz=now.tzinfo)
        state = {"day": day, "start": s.isoformat(), "end": e.isoformat(), "given": [str(start), str(end)],
                 "taps": state.get("taps", [])[-KEEP_TAPS:]}
        _save(path, state)
    s, e = dt.datetime.fromisoformat(state["start"]), dt.datetime.fromisoformat(state["end"])
    window = [state["start"], state["end"]]
    if not (s <= now < e):
        return {"did": "outside", "window": window}
    last = state["taps"][-1] if state["taps"] else None
    if last is not None and (now - dt.datetime.fromisoformat(last)).total_seconds() < interval_s:
        return {"did": "waiting", "window": window, "last_tap": last}
    person = None
    if idle is not None:
        person = person_idle(idle(), path, now=now)
        if person is not None and person < interval_s:
            return {"did": "active", "window": window, "idle_s": round(person, 1)}
    ok, detail = (do_tap or tap)()
    if not ok:
        return {"did": "busy" if detail.startswith("busy") else "failed", "window": window, "detail": detail}
    state["taps"].append(now.isoformat(timespec="seconds"))
    # a tap made with no idle reading still happens only after `interval_s` of idle time when a reader was given,
    # and with no reader nothing is claimed about the person, so the interval is the least it can have been
    state["last_tap"] = {"at": now.isoformat(), "idle_before": person if person is not None else float(interval_s)}
    _save(path, state)
    return {"did": "tapped", "window": window, "taps_today": sum(t[:10] == day for t in state["taps"])}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="claude-human keep-active", description=__doc__.split("\n\n")[0])
    p.add_argument("--start", help="the day's first hour, HH:MM in this Mac's time zone")
    p.add_argument("--end", help="the day's last hour, HH:MM")
    p.add_argument("--off", action="store_true", help="this day has no window")
    p.add_argument("--idle", action="store_true", help="print the system's idle seconds and exit")
    p.add_argument("--person-idle", action="store_true",
                   help="print the person's idle seconds, with this module's taps left out, and exit")
    p.add_argument("--spread", type=int, default=SPREAD_MIN, help="minutes either side of each hour (default 60)")
    p.add_argument("--interval", type=int, default=INTERVAL_S, help="seconds between taps (default 240)")
    p.add_argument("--state", default=DEFAULT_STATE, help="where the drawn window and the taps are kept")
    p.add_argument("--vhid", help="path to vhid_type")
    a = p.parse_args(argv)
    if a.idle:
        v = idle_seconds()
        print("unknown" if v is None else round(v, 1))
        return 0 if v is not None else 1
    if a.person_idle:
        v = person_idle(idle_seconds(), a.state)
        print("unknown" if v is None else round(v, 1))
        return 0 if v is not None else 1
    if a.off == bool(a.start or a.end) or bool(a.start) != bool(a.end):
        p.error("give --start and --end together, or --off")
    now = dt.datetime.now().astimezone()
    try:
        out = step(now, start=None if a.off else a.start, end=None if a.off else a.end, state_path=a.state,
                   spread_min=a.spread, interval_s=a.interval,
                   do_tap=lambda: tap(helper=a.vhid), idle=idle_seconds)
    except WindowError as e:
        print(f"keep-active: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 1 if out["did"] == "failed" else 0
