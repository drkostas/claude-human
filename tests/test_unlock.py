"""The unlock logic without a daemon, a lock screen or a panel. The helper is a fake runner."""
import subprocess
import types

import pytest

from claude_human import unlock as u


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        self.t += 1.0
        return self.t


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = types.SimpleNamespace(calls=[], locked=True, prompt={"wid": 7}, on_run=None, rc=0,
                              stdout="connected\ntyped 5 chars\nOK", popen=[], front=u.LOGIN_WINDOW)
    monkeypatch.setattr(u, "HID_LOCK", str(tmp_path / "hid.lock"))
    monkeypatch.setattr(u, "front_app", lambda **k: w.front)

    def run(argv, **kw):
        w.calls.append((argv, kw))
        if w.on_run:
            w.on_run()
        return types.SimpleNamespace(returncode=w.rc, stdout=w.stdout, stderr="err")

    clock = Clock()
    monkeypatch.setattr(u, "_run", run)
    monkeypatch.setattr(u, "_popen", lambda argv, **k: w.popen.append(argv))
    monkeypatch.setattr(u, "_sleep", lambda s: None)
    monkeypatch.setattr(u, "_now", clock.now)
    monkeypatch.setattr(u, "_exists", lambda p: True)
    w.is_locked = lambda: w.locked
    w.prompt_present = lambda: w.prompt
    return w


def test_helper_argv_uses_sudo_and_rejects_unknown_flags():
    assert u.helper_argv("/b/vhid", "--wake", "--return") == ["sudo", "-n", "/b/vhid", "--wake", "--return"]
    assert u.helper_argv("/b/vhid", sudo=False) == ["/b/vhid"]
    with pytest.raises(ValueError):
        u.helper_argv("/b/vhid", "--password=x")


# ---------------------------------------------------------------- unlock

def test_empty_password_is_refused_without_running_anything(world):
    assert u.unlock("", helper="/b/vhid", locked=world.is_locked)[0] is False
    assert world.calls == []


def test_an_unlocked_session_is_never_typed_into(world):
    world.locked = False
    assert u.unlock("hunter2", helper="/b/vhid", locked=world.is_locked) == (True, "Already unlocked.")
    assert world.calls == []


def test_a_missing_helper_is_reported(world, monkeypatch):
    monkeypatch.setattr(u, "_exists", lambda p: False)
    ok, detail = u.unlock("hunter2", helper="/no/vhid", locked=world.is_locked)
    assert not ok and "not built" in detail and world.calls == []


def test_the_password_goes_on_stdin_and_the_lock_is_observed(world):
    world.on_run = lambda: setattr(world, "locked", False)
    assert u.unlock("s3cr3t!", helper="/b/vhid", locked=world.is_locked) == (True, "Unlocked.")
    argv, kw = world.calls[-1]
    assert argv == ["sudo", "-n", "/b/vhid", "--wake", "--clear", "--return"]
    assert kw["input"] == "s3cr3t!" and all("s3cr3t!" not in a for a in argv)
    assert world.popen and world.popen[0][0] == "/usr/bin/caffeinate"


def test_typed_but_still_locked_is_a_failure_after_two_tries(world):
    ok, detail = u.unlock("s3cr3t!", helper="/b/vhid", locked=world.is_locked)
    assert not ok and "still locked" in detail
    assert len(world.calls) == 2


def test_the_second_try_is_reported(world):
    tries = []

    def second():
        tries.append(1)
        if len(tries) == 2:
            world.locked = False
    world.on_run = second
    assert u.unlock("pw", helper="/b/vhid", locked=world.is_locked) == (True, "Unlocked (took a second try).")


def test_a_helper_timeout_stops_at_once(world, monkeypatch):
    def boom(argv, **kw):
        world.calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 45)
    monkeypatch.setattr(u, "_run", boom)
    ok, detail = u.unlock("pw", helper="/b/vhid", locked=world.is_locked)
    assert not ok and "timed out" in detail and len(world.calls) == 1


# ---------------------------------------------------------------- relock

def test_relock_does_nothing_when_already_locked(world):
    assert u.relock(locked=world.is_locked) == (True, "Already locked.")
    assert world.calls == []


def test_relock_reports_a_failing_pmset(world):
    world.locked, world.rc, world.stdout = False, 1, ""
    ok, detail = u.relock(locked=world.is_locked)
    assert not ok and "pmset failed" in detail
    assert world.calls[-1][0] == ["/usr/bin/pmset", "displaysleepnow"]


def test_relock_waits_for_the_lock_bit(world):
    world.locked = False
    world.on_run = lambda: setattr(world, "locked", True)
    assert u.relock(locked=world.is_locked) == (True, "Locked.")


def test_relock_fails_when_the_lock_never_comes_or_cannot_be_read(world):
    world.locked = False
    assert "did not lock" in u.relock(poll_seconds=3, locked=world.is_locked)[1]
    states = iter([False, None])
    assert "cannot be observed" in u.relock(locked=lambda: next(states))[1]


# ---------------------------------------------------------------- password panels

def test_approve_refuses_without_a_panel(world):
    world.prompt = None
    ok, detail = u.approve("pw", helper="/b/vhid", prompt_present=world.prompt_present)
    assert not ok and detail == u.NO_PROMPT and world.calls == []
    assert u.approve("", helper="/b/vhid", prompt_present=world.prompt_present)[0] is False


def test_approve_types_password_tab_tab_space_and_watches_the_panel_close(world):
    world.on_run = lambda: setattr(world, "prompt", None)
    assert u.approve("s3cr3t!", helper="/b/vhid", prompt_present=world.prompt_present) == (True, "Approved.")
    argv, kw = world.calls[-1]
    assert argv == ["sudo", "-n", "/b/vhid", "--clear"]
    assert kw["input"] == "s3cr3t!\t\t " and all("s3cr3t!" not in a for a in argv)


def test_approve_with_the_panel_still_open_is_a_failure(world):
    ok, detail = u.approve("pw", helper="/b/vhid", prompt_present=world.prompt_present)
    assert not ok and "still open" in detail


def test_use_password_presses_return_only(world):
    assert u.use_password(helper="/b/vhid", prompt_present=world.prompt_present)[0] is True
    argv, kw = world.calls[-1]
    assert argv == ["sudo", "-n", "/b/vhid", "--return"] and kw["input"] == ""
    world.prompt = None
    world.calls.clear()
    assert u.use_password(helper="/b/vhid", prompt_present=world.prompt_present)[1] == u.NO_PROMPT
    assert world.calls == []


# ---------------------------------------------------------------- one typist, at the lock screen only

def test_nothing_is_typed_unless_the_lock_screen_is_in_front(world):
    world.front = "com.getmailspring.mailspring"
    ok, detail = u.unlock("pw", helper="/b/vhid", locked=world.is_locked)
    assert not ok and "not in front" in detail and "mailspring" in detail
    assert world.calls == []
    world.front = None
    ok, detail = u.unlock("pw", helper="/b/vhid", locked=world.is_locked)
    assert not ok and "cannot be read" in detail and world.calls == []


def test_a_mac_unlocked_during_the_wait_is_not_typed_into(world, monkeypatch):
    # the session reads locked when unlock starts and unlocked after its wake and wait
    monkeypatch.setattr(u, "_sleep", lambda s: setattr(world, "locked", False))
    assert u.unlock("pw", helper="/b/vhid", locked=world.is_locked) == (True, "Already unlocked.")
    assert world.calls == []


def test_two_unlocks_that_overlap_type_once(world, monkeypatch):
    """The second caller read locked before the first finished. It waits for the virtual keyboard, then
    finds the Mac unlocked and types nothing (no Cmd+A, Backspace, password or Return)."""
    import threading
    import time as _time
    typing = threading.Event()
    go_on = threading.Event()

    def run(argv, **kw):
        world.calls.append((argv, kw))
        typing.set()
        go_on.wait(5)
        world.locked = False          # the first unlock worked
        return types.SimpleNamespace(returncode=0, stdout="typed", stderr="")
    monkeypatch.setattr(u, "_run", run)
    out = {}
    first = threading.Thread(target=lambda: out.__setitem__(1, u.unlock("pw", helper="/b/vhid", locked=world.is_locked)))
    first.start()
    assert typing.wait(5)
    # the second reads locked here, as the window probe did, and then has to wait for the keyboard
    second = threading.Thread(target=lambda: out.__setitem__(2, u.unlock("pw", helper="/b/vhid", locked=world.is_locked)))
    second.start()
    _time.sleep(0.3)
    assert 2 not in out, "the second unlock waits while the first types"
    go_on.set()
    first.join(5)
    second.join(10)
    assert out[1] == (True, "Unlocked.") and out[2] == (True, "Already unlocked.")
    assert len(world.calls) == 1, "only the first unlock typed"


def test_a_busy_keyboard_is_refused_with_its_holder(world):
    lock = u.hid_lock()
    with lock.hold("someone else typing", 1):
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__(0, u.unlock("pw", helper="/b/vhid", locked=world.is_locked,
                                                                         wait_s=0.2)))
        t.start()
        t.join(5)
    ok, detail = out[0]
    assert not ok and "someone else typing" in detail and world.calls == []


def test_approve_looks_at_the_panel_again_once_it_holds_the_keyboard(world):
    seen = iter([{"wid": 7}, None])
    ok, detail = u.approve("pw", helper="/b/vhid", prompt_present=lambda: next(seen))
    assert not ok and detail == u.NO_PROMPT and world.calls == []
