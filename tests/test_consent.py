"""The consent prompt logic without a prompt, a screen or a keyboard. The helper is a fake runner."""
import types

import pytest

from claude_human import consent as c


def win(owner="UserNotificationCenter", wid=5, pid=42, w=260, h=256, alpha=1):
    return {"kCGWindowOwnerName": owner, "kCGWindowNumber": wid, "kCGWindowOwnerPID": pid,
            "kCGWindowAlpha": alpha, "kCGWindowBounds": {"X": 766, "Y": 222, "Width": w, "Height": h}}


def test_find_prompts_keeps_only_a_floating_panel_of_the_owner():
    raw = [win(owner="Finder"), win(alpha=0), win(w=40, h=40), win(w=1700, h=1100), win(wid=9)]
    found = c.find_prompts(raw, (1728, 1117))
    assert [p["wid"] for p in found] == [9]
    assert found[0]["pid"] == 42 and found[0]["w"] == 260


def test_join_lines_drops_buttons_and_straightens_quotes():
    lines = ["?", "“bash” wants access to control", "“System Events”.", "Don’t Allow", "Allow"]
    assert c.join_lines(lines) == '"bash" wants access to control "System Events".'


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        self.t += 1.0
        return self.t


@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(calls=[], open=[{"wid": 5, "pid": 42}], rc=0, closes=True)

    def run(argv, **kw):
        w.calls.append((argv, kw))
        if w.closes:
            w.open = []
        return types.SimpleNamespace(returncode=w.rc, stdout="typed", stderr="err")

    monkeypatch.setattr(c, "_run", run)
    monkeypatch.setattr(c, "_sleep", lambda s: None)
    monkeypatch.setattr(c, "_now", Clock().now)
    monkeypatch.setattr(c, "_exists", lambda p: True)
    w.find = lambda: w.open
    w.fronted = []
    w.forward = lambda pid: w.fronted.append(pid) or True
    return w


def test_allow_presses_return_and_sees_the_prompt_close(world):
    ok, why = c.answer("allow", helper="/h", find=world.find, forward=world.forward, sudo=False)
    assert ok and why == "Allowed."
    argv, kw = world.calls[0]
    assert argv == ["/h", "--return"] and kw["input"] == ""
    assert world.fronted == [42]


def test_deny_presses_space_on_the_focused_button(world):
    ok, _ = c.answer("deny", helper="/h", find=world.find, forward=world.forward, sudo=False)
    argv, kw = world.calls[0]
    assert ok and argv == ["/h"] and kw["input"] == " "


def test_nothing_is_pressed_without_a_prompt(world):
    world.open = []
    ok, why = c.answer("allow", helper="/h", find=world.find, forward=world.forward)
    assert not ok and "No consent prompt" in why and world.calls == []


def test_nothing_is_pressed_when_the_list_cannot_be_read(world):
    ok, _ = c.answer("allow", helper="/h", find=lambda: None, forward=world.forward)
    assert not ok and world.calls == []


def test_nothing_is_pressed_when_the_prompt_cannot_be_brought_forward(world):
    ok, why = c.answer("allow", helper="/h", find=world.find, forward=lambda pid: False)
    assert not ok and "front" in why and world.calls == []


def test_a_prompt_still_open_after_the_key_is_a_failure(world):
    world.closes = False
    ok, why = c.answer("allow", helper="/h", find=world.find, forward=world.forward, sudo=False)
    assert not ok and "still open" in why


def test_an_unknown_answer_is_refused(world):
    ok, _ = c.answer("maybe", helper="/h", find=world.find, forward=world.forward)
    assert not ok and world.calls == []
