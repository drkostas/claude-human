"""keep_active: the window drawn for a day, one pass of the scheduler, and the tap's lock.

No keyboard is touched. The tap is a stand-in, and the helper in the lock tests is a script under tmp_path."""
import datetime as dt
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_human import keep_active as ka
from claude_human.session_lock import SessionLock

ZONE = dt.timezone(dt.timedelta(hours=2))
DAY = dt.date(2026, 11, 23)


def at(h, m=0, day=DAY):
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ZONE)


def test_the_window_is_within_the_spread_and_differs_between_days():
    seen = set()
    for d in range(30):
        day = DAY + dt.timedelta(days=d)
        s, e = ka.draw_window(day, "09:00", "19:00", rng=random.Random(d), tz=ZONE)
        assert abs((s - at(9, 0, day)).total_seconds()) <= 3600
        assert abs((e - at(19, 0, day)).total_seconds()) <= 3600
        seen.add((s.time(), e.time()))
    assert len(seen) > 10


def test_hours_that_leave_no_window_are_refused_and_a_crossing_draw_is_moved_back():
    with pytest.raises(ka.WindowError):
        ka.draw_window(DAY, "09:00", "09:10", tz=ZONE)
    with pytest.raises(ka.WindowError):
        ka.draw_window(DAY, "nine", "19:00", tz=ZONE)
    for seed in range(200):
        s, e = ka.draw_window(DAY, "09:00", "10:00", spread_min=60, rng=random.Random(seed), tz=ZONE)
        assert (e - s).total_seconds() >= ka.MIN_WINDOW_S


def run_day(tmp_path, taps, *, start="09:00", end="19:00", interval=240, minutes=range(7 * 60, 21 * 60),
            result=(True, "tapped")):
    state = tmp_path / "state.json"
    did = []
    for m in minutes:
        now = at(0, 0) + dt.timedelta(minutes=m)
        out = ka.step(now, start=start, end=end, state_path=state, interval_s=interval, rng=random.Random(5),
                      do_tap=lambda now=now: (taps.append(now) or result))
        did.append(out["did"])
    return did, json.loads(state.read_text())


def test_a_day_taps_only_inside_the_drawn_window_and_at_the_interval(tmp_path):
    taps = []
    did, state = run_day(tmp_path, taps)
    s, e = dt.datetime.fromisoformat(state["start"]), dt.datetime.fromisoformat(state["end"])
    assert taps and all(s <= t < e for t in taps)
    gaps = [(b - a).total_seconds() for a, b in zip(taps, taps[1:])]
    assert min(gaps) >= 240 and max(gaps) <= 300
    assert set(did) == {"outside", "tapped", "waiting"}
    assert len(state["taps"]) == len(taps)


def test_the_window_drawn_for_a_day_stays_when_other_hours_are_given_later_that_day(tmp_path):
    state = tmp_path / "state.json"
    ka.step(at(12), start="09:00", end="19:00", state_path=state, rng=random.Random(1), do_tap=lambda: (True, "tapped"))
    first = json.loads(state.read_text())
    ka.step(at(12, 5), start="10:00", end="12:00", state_path=state, rng=random.Random(2),
            do_tap=lambda: (True, "tapped"))
    assert json.loads(state.read_text())["start"] == first["start"]


def test_a_day_with_no_window_never_taps(tmp_path):
    taps = []
    state = tmp_path / "state.json"
    out = ka.step(at(12), start=None, end=None, state_path=state, do_tap=lambda: taps.append(1) or (True, "tapped"))
    assert out == {"did": "off", "window": None} and taps == [] and not state.exists()


def test_a_busy_keyboard_skips_the_tap_and_tries_at_the_next_pass(tmp_path):
    taps = []
    state = tmp_path / "state.json"
    out = ka.step(at(12), start="09:00", end="19:00", state_path=state, spread_min=0,
                  do_tap=lambda: (taps.append(1) or (False, "busy: another process is typing")))
    assert out["did"] == "busy"
    out = ka.step(at(12, 1), start="09:00", end="19:00", state_path=state, spread_min=0,
                  do_tap=lambda: (taps.append(1) or (True, "tapped")))
    assert out["did"] == "tapped" and len(taps) == 2
    assert len(json.loads(state.read_text())["taps"]) == 1


def test_a_failed_tap_is_not_recorded_and_says_so(tmp_path):
    taps = []
    did, state = run_day(tmp_path, taps, minutes=[12 * 60], result=(False, "the virtual keyboard helper failed: x"))
    assert did == ["failed"] and state["taps"] == []


def helper_script(tmp_path, body):
    p = tmp_path / "vhid_fake"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


def test_tap_runs_the_helper_with_only_the_wake_flag_and_nothing_on_stdin(tmp_path):
    seen = tmp_path / "seen"
    h = helper_script(tmp_path, f'cat > {seen}.in; echo "$@" > {seen}.args; echo typed 0 chars\n')
    ok, detail = ka.tap(helper=h, sudo=False, lock=SessionLock(tmp_path / "hid.lock"))
    assert ok and detail == "tapped"
    assert (tmp_path / "seen.args").read_text().strip() == "--wake"
    assert (tmp_path / "seen.in").read_text() == ""


def test_tap_does_not_wait_while_another_process_holds_the_keyboard(tmp_path):
    h = helper_script(tmp_path, "exit 0\n")
    src = str(Path(ka.__file__).parents[1])
    code = ("import sys,time;sys.path.insert(0,%r);from claude_human.session_lock import SessionLock;"
            "l=SessionLock(%r);h,w=l.take('typing',5);print('held',flush=True);time.sleep(8)"
            % (src, str(tmp_path / "hid.lock")))
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        ok, detail = ka.tap(helper=h, sudo=False, lock=SessionLock(tmp_path / "hid.lock"))
        assert not ok and detail.startswith("busy") and time.monotonic() - t0 < 3
    finally:
        holder.kill()


def test_a_helper_that_fails_or_cannot_start_is_a_failure_with_words(tmp_path):
    lock = SessionLock(tmp_path / "hid.lock")
    ok, detail = ka.tap(helper=helper_script(tmp_path, "echo no >&2; exit 3\n"), sudo=False, lock=lock)
    assert not ok and "failed" in detail and "no" in detail
    ok, detail = ka.tap(helper=tmp_path / "missing", sudo=False, lock=lock)
    assert not ok and "could not run" in detail
