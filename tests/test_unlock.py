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
def world(monkeypatch):
    w = types.SimpleNamespace(calls=[], locked=True, prompt={"wid": 7}, on_run=None, rc=0,
                              stdout="connected\ntyped 5 chars\nOK", popen=[])

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
