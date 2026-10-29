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


#: Gatekeeper's '"OpenMTP" is an app downloaded from the Internet' alert as the window list showed it
#: (macOS 26, a 1728 by 1117 display), behind the lock screen's two windows
GATEKEEPER = [
    {"kCGWindowOwnerName": "loginwindow", "kCGWindowNumber": 14351, "kCGWindowOwnerPID": 501, "kCGWindowLayer": 2004,
     "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1728, "Height": 1117}},
    {"kCGWindowOwnerName": "Dock", "kCGWindowNumber": 70, "kCGWindowOwnerPID": 600, "kCGWindowLayer": 20,
     "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1728, "Height": 1117}},
    {"kCGWindowOwnerName": "CoreServicesUIAgent", "kCGWindowNumber": 14369, "kCGWindowOwnerPID": 58901,
     "kCGWindowLayer": 8, "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 766, "Y": 236, "Width": 260, "Height": 274}},
]


def test_gatekeepers_alert_is_a_prompt_with_its_own_buttons():
    found = c.find_prompts(GATEKEEPER, (1728, 1117))
    assert [(p["wid"], p["owner"], p["allow"], p["deny"]) for p in found] == [(14369, "CoreServicesUIAgent", "Open", "Cancel")]
    assert c.find_prompts([win()], (1728, 1117))[0]["deny"] == "Don't Allow"
    assert c.join_lines(['“OpenMTP” is an app downloaded from the Internet.', "Are you sure you want to open it?",
                         "Open", "Cancel"]) == '"OpenMTP" is an app downloaded from the Internet. Are you sure you want to open it?'


def test_a_gatekeeper_prompt_is_pressed_by_its_buttons_title_without_the_keyboard(world):
    world.open = [{"wid": 14369, "pid": 58901, "owner": "CoreServicesUIAgent"}]
    pressed = []

    def press(pid, wid, title):
        pressed.append((pid, wid, title))
        world.open = []
        return True, f"pressed {title}"
    ok, why = c.answer("deny", helper="/h", find=world.find, forward=world.forward, press=press)
    assert ok and why == "Pressed Cancel." and pressed == [(58901, 14369, "Cancel")]
    assert world.calls == [] and world.fronted == []   # no helper, no bringing forward
    world.open = [{"wid": 14369, "pid": 58901, "owner": "CoreServicesUIAgent"}]
    ok, why = c.answer("allow", find=world.find, press=press)
    assert ok and pressed[-1][2] == "Open"


def test_a_gatekeeper_press_that_fails_or_leaves_it_open_says_so(world):
    world.open = [{"wid": 14369, "pid": 58901, "owner": "CoreServicesUIAgent"}]
    ok, why = c.answer("allow", find=world.find, press=lambda *a: (False, "this process has no Accessibility grant"))
    assert not ok and why.startswith("nothing was pressed") and "grant" in why
    ok, why = c.answer("allow", find=world.find, press=lambda *a: (True, "pressed Open"))
    assert not ok and "still open" in why


class FakeAX:
    """The Accessibility API over a tree of dicts: {"role", "title", "children", "wid"}."""

    def __init__(self, windows, trusted=True):
        self.app = {"role": "AXApplication", "title": "CoreServicesUIAgent", "children": windows}
        self.trusted, self.pressed = trusted, []

    def AXIsProcessTrusted(self):
        return self.trusted

    def AXUIElementCreateApplication(self, pid):
        return self.app

    def AXUIElementCopyAttributeValue(self, element, name, _):
        value = {"AXWindows": element.get("children") if element is self.app else None,
                 "AXRole": element.get("role"), "AXTitle": element.get("title"),
                 "AXChildren": element.get("children")}.get(name)
        return (0, value) if value is not None else (-25205, None)

    def AXUIElementPerformAction(self, element, action):
        self.pressed.append((element["title"], action))
        return 0

    def window_id(self, element):
        return element.get("wid")


def alert(wid, *buttons):
    group = {"role": "AXGroup", "children": [{"role": "AXButton", "title": b} for b in buttons]}
    return {"role": "AXWindow", "title": "", "wid": wid, "children": [{"role": "AXStaticText", "title": ""}, group]}


def test_press_button_finds_the_button_by_title_in_the_named_window_only():
    ax = FakeAX([alert(1, "Open", "Cancel"), alert(14369, "Open", "Cancel")])
    assert c.press_button(58901, 14369, "Cancel", ax=ax) == (True, "pressed Cancel")
    assert ax.pressed == [("Cancel", "AXPress")]
    ax = FakeAX([alert(1, "Open", "Cancel")])
    ok, why = c.press_button(58901, 14369, "Cancel", ax=ax)
    assert not ok and "not among" in why and ax.pressed == []
    ax = FakeAX([alert(14369, "Move to Trash", "Done")])
    ok, why = c.press_button(58901, 14369, "Open", ax=ax)
    assert not ok and "no button titled 'Open'" in why and ax.pressed == []


def test_press_button_without_the_accessibility_grant_presses_nothing():
    ax = FakeAX([alert(14369, "Open", "Cancel")], trusted=False)
    ok, why = c.press_button(58901, 14369, "Open", ax=ax)
    assert not ok and "grant" in why and ax.pressed == []


def test_a_title_with_a_curly_apostrophe_matches():
    ax = FakeAX([alert(5, "Don’t Allow", "Allow")])
    assert c.press_button(1, 5, "Don't Allow", ax=ax)[0]


def test_ask_station_posts_a_consent_input_with_the_master_token():
    import io
    import json
    import urllib.error
    sent = []

    def opener(req, timeout=None):
        sent.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return io.BytesIO(json.dumps({"ok": True, "detail": "Pressed Cancel."}).encode())
    assert c.ask_station("deny", token="tok", opener=opener) == (True, "Pressed Cancel.")
    assert sent == [("http://127.0.0.1:8789/input", "Bearer tok", {"type": "consent", "choice": "deny"})]

    def refused(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)
    assert c.ask_station("allow", token="tok", opener=refused) == (False, "the station answered 403")
    assert not c.ask_station("open", token="tok", opener=opener)[0] and len(sent) == 1


def test_a_capture_that_saves_no_file_says_why(world, monkeypatch):
    prompt = {"x": 1, "y": 2, "w": 300, "h": 200}
    assert c.capture_why(prompt, "/tmp/x.png") is None and c.capture(prompt, "/tmp/x.png")
    monkeypatch.setattr(c, "_exists", lambda p: False)
    why = c.capture_why(prompt, "/tmp/x.png")
    assert why and "wrote no file" in why and not c.capture(prompt, "/tmp/x.png")
    world.rc = 1
    assert c.capture_why(prompt, "/tmp/x.png").startswith("screencapture exited 1")


def test_consent_list_with_out_exits_1_when_no_picture_was_saved(monkeypatch, capsys):
    from claude_human import cli
    monkeypatch.setattr(c, "prompts", lambda: [{"owner": "UserNotificationCenter", "x": 1, "y": 2, "w": 300, "h": 200}])
    monkeypatch.setattr(c, "capture_why", lambda p, out, env=None: "screencapture wrote no file")
    assert cli.main(["consent", "list", "--out", "/tmp/x.png"]) == 1
    out = capsys.readouterr()
    assert "no picture saved to /tmp/x.png" in out.err and '"image_error"' in out.out
    monkeypatch.setattr(c, "capture_why", lambda p, out, env=None: None)
    monkeypatch.setattr(c, "read_text", lambda p: "words")
    assert cli.main(["consent", "list", "--out", "/tmp/x.png"]) == 0


def test_any_owners_prompt_is_pressed_by_title_on_request_and_tried_once_more(world):
    unc = {"wid": 5, "pid": 42, "owner": "UserNotificationCenter"}
    world.open, pressed = [dict(unc)], []

    def stubborn(pid, wid, title):              # the first press leaves it open, the second closes it
        pressed.append(title)
        if len(pressed) == 2:
            world.open = []
        return True, f"pressed {title}"
    ok, why = c.answer("allow", find=world.find, press=stubborn, by_title=True)
    assert ok and why == "Pressed Allow (on try 2)." and pressed == ["Allow", "Allow"] and world.calls == []
    world.open, pressed = [dict(unc)], []
    ok, why = c.answer("deny", find=world.find, press=lambda p, w, t: pressed.append(t) or (True, "pressed"), by_title=True)
    assert not ok and "2 times" in why and pressed == ["Don't Allow", "Don't Allow"]
    world.open = [dict(unc)]
    ok, why = c.answer("allow", find=world.find, forward=world.forward, helper="/h", sudo=False)
    assert ok and world.calls and world.calls[0][0] == ["/h", "--return"]   # without by_title, the keyboard as before


class Texts(FakeAX):
    def __init__(self, windows, trusted=True):
        super().__init__(windows, trusted)

    def AXUIElementCopyAttributeValue(self, element, name, _):
        if name == "AXValue":
            return (0, element.get("value")) if element.get("value") is not None else (-25205, None)
        return super().AXUIElementCopyAttributeValue(element, name, _)


def test_read_words_reads_the_prompts_text_and_none_without_the_grant():
    win = {"role": "AXWindow", "wid": 5, "children": [
        {"role": "AXStaticText", "value": "\u201ctool\u201d wants access to control \u201cBrowser\u201d."},
        {"role": "AXStaticText", "value": ""}, {"role": "AXButton", "title": "Allow"}]}
    assert c.read_words(42, 5, ax=Texts([win])) == '"tool" wants access to control "Browser".'
    assert c.read_words(42, 5, ax=Texts([win], trusted=False)) is None
    assert c.read_words(42, 9, ax=Texts([win])) is None
