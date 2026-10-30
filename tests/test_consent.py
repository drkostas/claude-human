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


#: an app asking to control the computer through accessibility features, as the window list showed
#: it on a desktop nobody was looking at, behind an ordinary window on screen
ACCESSIBILITY = [
    {"kCGWindowOwnerName": "Notes", "kCGWindowNumber": 3, "kCGWindowOwnerPID": 300, "kCGWindowLayer": 0,
     "kCGWindowAlpha": 1.0, "kCGWindowIsOnscreen": True, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 900, "Height": 700}},
    {"kCGWindowOwnerName": "universalAccessAuthWarn", "kCGWindowNumber": 16507, "kCGWindowOwnerPID": 20651,
     "kCGWindowLayer": 8, "kCGWindowAlpha": 1.0, "kCGWindowIsOnscreen": False,
     "kCGWindowBounds": {"X": 734, "Y": 260, "Width": 260, "Height": 300}},
]


def test_an_accessibility_request_is_a_prompt_only_the_person_grants_in_system_settings():
    found = c.find_prompts(ACCESSIBILITY, (1728, 1117))
    assert [(p["wid"], p["kind"], p["on_screen"], p["allow"], p["deny"]) for p in found] == \
        [(16507, "settings", False, "Open System Settings", "Deny")]
    assert c.find_prompts([win()], (1728, 1117))[0]["kind"] == "press"
    assert c.find_prompts([win()], (1728, 1117))[0]["on_screen"] is True


def test_allowing_an_accessibility_request_is_refused_and_presses_nothing(world):
    world.open = [{"wid": 16507, "pid": 20651, "owner": "universalAccessAuthWarn"}]
    pressed = []
    ok, why = c.answer("allow", find=world.find, forward=world.forward, helper="/h",
                       press=lambda *a: pressed.append(a) or (True, "pressed"), by_title=True)
    assert not ok and "System Settings" in why and pressed == [] and world.calls == []
    def press(pid, wid, title):
        pressed.append(title)
        world.open = []
        return True, "pressed"
    ok, why = c.answer("deny", find=world.find, press=press, by_title=True)
    assert not ok and pressed == []     # never chosen without being named
    ok, why = c.answer("deny", find=world.find, press=press, by_title=True, wid=16507)
    assert ok and pressed == ["Deny"]   # a deny the caller was told to give, for that window, presses Deny


#: the unified log (compact style) when one program asked through another to control an app: the
#: target app logs the request, and the identity block names the program that sent it Apple Events
LOG = """2026-01-31 23:28:41.188 A  osascript[87135:e2a0f4b] (TCC) TCCAccessRequest() IPC
2026-01-31 23:28:42.047 Df System Events[88343:e2a103b] [com.apple.TCC:access] TCCAccessRequestIndirect: TCCAccessRequestIndirect with pid 88343: target_identity: {
    kTCCCodeIdentityAuthority = kTCCCodeIdentityDesignatedRequirementAuthority;
    kTCCCodeIdentityExecutableURL = "file:///usr/local/bin/tool";
    kTCCCodeIdentityIdentifier = "/usr/local/bin/tool";
    kTCCCodeIdentityTeamID = "";
}
2026-01-31 23:28:42.047 A  System Events[88343:e2a103b] (TCC) TCCAccessRequestIndirect() IPC
2026-01-31 23:29:10.000 Df Example Browser[37348:e2a0cf7] [com.apple.TCC:access] TCCAccessRequestIndirect: TCCAccessRequestIndirect with pid 37348: target_identity: {
    kTCCCodeIdentityIdentifier = "com.example.terminal";
}
2026-01-31 23:29:10.020 Df Example Browser[37348:e2a0053] [com.apple.TCC:access] TCCAccessRequestIndirect_block_invoke: access request result was 1, timeout was 0
"""


def test_requests_name_the_asker_and_the_target_from_the_log():
    got = c.requests(LOG)
    assert [(r["target"], r["target_pid"], r["asker"]) for r in got] == [
        ("System Events", 88343, "/usr/local/bin/tool"), ("Example Browser", 37348, "com.example.terminal")]
    assert got[0]["at"] == "2026-01-31 23:28:42.047"
    assert [r["result"] for r in got] == [None, 1]                       # the first still waits on a prompt
    assert [r["asker"] for r in c.waiting(got)] == ["/usr/local/bin/tool"]
    assert c.short("/usr/local/bin/tool") == "tool" and c.short("com.example.terminal") == "com.example.terminal"
    assert c.requests("") == [] and c.short(None) is None


def test_recent_requests_runs_the_log_and_none_when_it_fails():
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=LOG)
    assert len(c.recent_requests(5, run=run)) == 2 and seen[0][:2] == ["/usr/bin/log", "show"] and "5m" in seen[0]
    assert c.recent_requests(run=lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="")) is None


def test_timed_out_reads_the_denials_nobody_answered_and_none_when_unreadable(tmp_path):
    import sqlite3
    db = tmp_path / "TCC.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE access (service TEXT, client TEXT, indirect_object_identifier TEXT, "
                 "auth_value INTEGER, auth_reason INTEGER, last_modified INTEGER)")
    conn.executemany("INSERT INTO access VALUES (?,?,?,?,?,?)", [
        ("kTCCServiceAppleEvents", "/usr/local/bin/tool", "com.example.events", 0, 9, 200),
        ("kTCCServiceAppleEvents", "com.example.terminal", "com.example.viewer", 0, 9, 100),
        ("kTCCServiceAppleEvents", "com.example.other", "com.example.events", 0, 2, 300),   # denied by a person
        ("kTCCServiceAppleEvents", "com.example.ok", "com.example.events", 2, 9, 400),      # allowed since
        ("kTCCServiceCamera", "com.example.cam", "UNUSED", 0, 9, 500)])
    conn.commit()
    conn.close()
    assert c.timed_out(db) == [{"asker": "/usr/local/bin/tool", "target": "com.example.events", "at": 200},
                               {"asker": "com.example.terminal", "target": "com.example.viewer", "at": 100}]
    assert c.timed_out(tmp_path / "missing.db") is None


def test_the_named_prompt_is_pressed_and_a_settings_prompt_in_front_is_passed_over(world):
    front = {"wid": 16921, "pid": 20651, "owner": "universalAccessAuthWarn"}
    behind = [{"wid": 16914, "pid": 42, "owner": "UserNotificationCenter"},
              {"wid": 16943, "pid": 42, "owner": "UserNotificationCenter"}]
    world.open, pressed = [front, *behind], []

    def press(pid, wid, title):
        pressed.append((wid, title))
        world.open = [p for p in world.open if p["wid"] != wid]
        return True, "pressed"
    ok, _ = c.answer("allow", find=world.find, press=press, by_title=True)
    assert ok and pressed == [(16914, "Allow")]          # the request in front is never the one pressed
    ok, _ = c.answer("deny", find=world.find, press=press, by_title=True, wid=16943)
    assert ok and pressed[-1] == (16943, "Don't Allow")
    ok, why = c.answer("allow", find=world.find, press=press, by_title=True, wid=16914)
    assert not ok and "no longer open" in why and len(pressed) == 2
    world.open = [front]
    ok, why = c.answer("allow", find=world.find, press=press, by_title=True)
    assert not ok and "System Settings" in why and len(pressed) == 2
