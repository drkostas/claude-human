"""The recipe runner with the Mac replaced: a fake window server, clock and subprocess."""
import subprocess


import pytest

from claude_human.station import prepare
from claude_human.station.prepare import Preparer, argv_phase, phase_of, succeeded


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += float(s)


class Mac:
    def __init__(self, app, *, present=True, running=True, elsewhere=False, refuse=False,
                 vanish_every=False, reopens=True):
        self.app, self.present, self.running, self.elsewhere = app, present, running, elsewhere
        self.refuse, self.vanish_every, self.reopens = refuse, vanish_every, reopens
        self.inputs, self.argv = [], []

    def windows(self):
        return [{"app": self.app, "width": 723, "height": 1084}] if self.present else []

    def all_windows(self):
        return [{"app": self.app}] if (self.present or self.elsewhere) else []

    def do_input(self, msg):
        self.inputs.append(msg)
        if self.vanish_every:
            self.present = False
        return {"ok": False, "locked": True} if self.refuse else {"ok": True}

    def run(self, argv, **kw):
        self.argv.append(list(argv))
        if argv[:2] == ["/usr/bin/pgrep", "-x"]:
            return subprocess.CompletedProcess(argv, 0 if self.running else 1, "", "")
        if argv[0] == "/usr/bin/open" and "-a" in argv and argv[-1] == self.app and self.reopens:
            self.present = self.running = True
        return subprocess.CompletedProcess(argv, 0, "", "")


def rig(app="System Settings", **kw):
    mac = Mac(app, **kw)
    return mac, Preparer(mac, all_windows=mac.all_windows, run=mac.run, clock=Clock())


def test_a_full_recipe_reports_each_step_and_ends_still_open():
    mac, p = rig()
    out = p.run("System Settings", ["open x-apple.systempreferences:x", "wait 1", "search Login", "key return"])
    assert succeeded(out) and out[0] == "System Settings: window present"
    assert ["/usr/bin/open", "-g", "x-apple.systempreferences:x"] in mac.argv
    assert [m["type"] for m in mac.inputs] == ["click", "key", "text", "key"]
    assert mac.inputs[0]["x"] == pytest.approx(132 / 723) and mac.inputs[1]["flags"] == 1048576


def test_a_refused_input_is_a_failed_step_after_one_retry():
    mac, p = rig(refuse=True)
    out = p.run("System Settings", ["key return"])
    assert not succeeded(out) and "the Mac is locked" in out[-1]
    assert len(mac.inputs) == 2


def test_place_phase_defers_input_and_never_opens_a_second_window():
    mac, p = rig("Demo", present=False, elsewhere=True)
    out = p.run("Demo", ["open x-apple.foo", "click 0.5 0.5"], phase="place")
    assert "click 0.5 0.5: deferred to the input phase" in out and succeeded(out)
    assert mac.inputs == [] and not any("-a" in a for a in mac.argv)


def test_a_dead_app_is_launched_in_the_background_in_the_place_phase():
    mac, p = rig("Demo", present=False, running=False)
    p.run("Demo", ["open x-apple.foo"], phase="place")
    assert mac.argv[1] == ["/usr/bin/open", "-g", "-a", "Demo"]


def test_a_window_that_vanishes_every_time_is_reported():
    mac, p = rig("Demo", vanish_every=True, reopens=False)
    out = p.run("Demo", ["click 0.1 0.1"])
    assert not succeeded(out)


def test_a_browser_url_gets_its_own_window_and_a_hung_browser_says_why():
    mac, p = rig("Brave Browser")
    p.run("Brave Browser", ["open https://example.com"])
    assert any(a[0] == "/usr/bin/osascript" and "make new window" in a[2] for a in mac.argv)

    def hang(argv, **kw):
        if argv[0] == "/usr/bin/osascript":
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return mac.run(argv, **kw)
    p.sh = hang
    out = p.run("Brave Browser", ["open https://example.com"], phase="place")
    assert not succeeded(out) and "dialog" in out[-1]


def test_phase_helpers():
    assert phase_of("open x") == "place" and phase_of("search y") == "input" and phase_of("# c") == "skip"
    assert argv_phase(["A", "--phase", "place", "open x"]) == ("place", ["A", "open x"])
    assert argv_phase(["A", "s"]) == ("all", ["A", "s"])
    with pytest.raises(SystemExit):
        argv_phase(["A", "--phase", "plce"])
    assert not succeeded([]) and not succeeded(["x: still open", "x: WINDOW DISAPPEARED"])
    assert prepare.KEYS["esc"] == prepare.KEYS["escape"] == 53


def test_unknown_search_field_and_key_fail_the_step():
    mac, p = rig("Demo")
    assert "no search field" in p.run("Demo", ["search x"])[-1]
    assert "unknown key" in p.run("Demo", ["key hyper"])[-1]
    with pytest.raises(ValueError):
        p.run("Demo", [], phase="later")


def test_the_default_all_windows_without_yabai_is_the_screen(monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: None)
    mac = Mac("Demo")
    p = Preparer(mac, run=mac.run, clock=Clock())
    assert p.all_windows == mac.windows
