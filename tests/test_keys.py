"""Key combinations pressed in one window through the station: parsed first, after an idle Mac, with
the window brought forward and the front window put back, and stopped at the first refusal."""
import io
import json
import urllib.error

import pytest

from claude_human import keys


def test_a_combination_is_a_key_code_and_its_modifier_flags():
    assert keys.parse("cmd+ctrl+left") == (123, 0x100000 | 0x40000)
    assert keys.parse("Right") == (124, 0)
    assert keys.parse("shift+alt+F12") == (111, 0x20000 | 0x80000)
    assert keys.parse("cmd+a") == (0, 0x100000) and keys.parse("cmd+v") == (9, 0x100000)
    assert keys.parse("escape") == keys.parse("esc") == (53, 0)
    for bad in ("", "cmd+", "cmd+ctrl+lft", "hyper+left", "cmd+cmd+left", "ctrl+control+up"):
        with pytest.raises(ValueError):
            keys.parse(bad)


def test_the_idle_time_is_read_from_the_hid_system():
    assert keys.idle_s('  |   "HIDIdleTime" = 2500000000\n') == 2.5
    assert keys.idle_s("nothing here") is None and keys.idle_s(None) is None


class Mac:
    """The commands press runs and the station it posts to, from a small model: how long the
    person has been idle at each look, which window is in front, and whether focus works."""

    def __init__(self, idle=(300.0,), front=7, focus_works=True, refuse=(), ioreg_works=True):
        self.idle, self.front, self.focus_works = list(idle), front, focus_works
        self.refuse, self.ioreg_works = set(refuse), ioreg_works
        self.ran, self.posted, self.slept = [], [], []

    def run(self, cmd, **k):
        class R:
            returncode, stdout = 0, ""
        r = R()
        self.ran.append(cmd)
        if cmd[0].endswith("ioreg"):
            if not self.ioreg_works:
                r.returncode = 1
                return r
            now = self.idle.pop(0) if len(self.idle) > 1 else self.idle[0]
            r.stdout = f'"HIDIdleTime" = {int(now * 1e9)}\n'
        elif cmd[1:] == ["-m", "query", "--windows", "--window"]:
            r.stdout = json.dumps({"id": self.front, "app": {7: "Notes", 42: "iTerm2"}.get(self.front, "App")})
        elif cmd[1:3] == ["-m", "window"] and cmd[-1] == "--focus":
            if self.focus_works:
                self.front = int(cmd[3])
        return r

    def opener(self, req, timeout=None):
        msg = json.loads(req.data)
        assert req.get_header("Authorization") == "Bearer tok" and req.full_url.endswith("/input")
        self.posted.append(msg)
        if msg["window"] != self.front:
            body = {"ok": False, "detail": "That window is not in front on the Mac."}
        elif (msg["code"], msg["flags"]) in self.refuse:
            body = {"ok": False, "detail": "a dialog is up"}
        else:
            body = {"ok": True}
        return io.BytesIO(json.dumps(body).encode())

    def sleep(self, s):
        self.slept.append(s)


def _press(mac, combos, **kw):
    return keys.press(42, combos, token="tok", run=mac.run, opener=mac.opener, sleep=mac.sleep,
                      clock=lambda: sum(mac.slept), **kw)


def test_it_waits_for_an_idle_mac_brings_the_window_forward_presses_and_puts_the_front_back():
    mac = Mac(idle=(3.0, 50.0, 130.0))
    got = _press(mac, ["cmd+ctrl+left", "cmd+ctrl+right"])
    assert got["ok"] and got["pressed"] == ["cmd+ctrl+left", "cmd+ctrl+right"] and got["why"] is None
    assert [(p["code"], p["flags"], p["window"]) for p in mac.posted] == [(123, 0x140000, 42), (124, 0x140000, 42)]
    assert got["front_before"] == {"id": 7, "app": "Notes"} and got["brought"] and got["restored"]
    assert mac.front == 7, "the window that was in front is in front again"
    assert sum(mac.slept) >= 2 and got["waited_s"] > 0, "nothing was posted before the Mac was idle"


def test_a_window_already_in_front_is_pressed_in_place():
    mac = Mac(front=42)
    got = _press(mac, ["return"])
    assert got["ok"] and not got["brought"] and not got["restored"] and mac.front == 42
    assert not any(c[-1] == "--focus" for c in mac.ran)


def test_it_refuses_rather_than_wait_for_ever_or_press_into_another_window():
    mac = Mac(idle=(1.0,))
    got = _press(mac, ["return"], wait=30)
    assert not got["ok"] and "in use" in got["why"] and not mac.posted
    mac = Mac(ioreg_works=False)
    got = _press(mac, ["return"])
    assert not got["ok"] and "idle" in got["why"] and not mac.posted
    mac = Mac(focus_works=False)
    got = _press(mac, ["return"])
    assert not got["ok"] and "did not come to the front" in got["why"] and not mac.posted
    mac = Mac()
    got = _press(mac, ["return"], bring=False)
    assert not got["ok"] and "not in front" in got["why"] and not mac.posted
    assert not any(c[-1] == "--focus" for c in mac.ran), "with bring False nothing moves the screen"


def test_it_stops_at_the_first_refusal_and_still_puts_the_front_back():
    mac = Mac(refuse={(124, 0)})
    got = _press(mac, ["left", "right", "up"])
    assert not got["ok"] and got["pressed"] == ["left"] and got["why"].startswith("right:")
    assert len(mac.posted) == 2 and got["restored"] and mac.front == 7


def test_every_combination_is_parsed_before_anything_waits_or_moves():
    mac = Mac()
    with pytest.raises(ValueError):
        _press(mac, ["left", "cmd+nonsense"])
    with pytest.raises(ValueError):
        _press(mac, [])
    assert not mac.ran and not mac.posted


def test_an_unreachable_station_is_a_refusal_with_a_reason():
    def down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    def unauthorized(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    assert keys.post(1, 123, 0, token="t", opener=down) == {
        "ok": False, "detail": "the station did not answer: URLError"}
    assert keys.post(1, 123, 0, token="t", opener=unauthorized) == {
        "ok": False, "detail": "the station answered 401"}


def test_the_command_refuses_without_a_token_or_with_a_name_it_does_not_know(tmp_path, capsys, monkeypatch):
    missing = str(tmp_path / "none")
    assert keys.main(["--window", "1", "--token-file", missing, "left"], environ={}) == 2
    assert "no station token" in capsys.readouterr().out
    tok = tmp_path / "tok"
    tok.write_text("tok\n")
    assert keys.main(["--window", "1", "--token-file", str(tok), "cmd+nonsense"], environ={}) == 2
    assert "unknown key" in capsys.readouterr().out
    seen = {}
    monkeypatch.setattr(keys, "press", lambda window, combos, **kw: seen.update(window=window, combos=combos, **kw)
                        or {"ok": True, "pressed": list(combos), "why": None})
    assert keys.main(["--window", "9", "cmd+ctrl+left"], environ={"CLAUDE_HUMAN_STATION_TOKEN": "env-tok"}) == 0
    assert seen["token"] == "env-tok" and seen["window"] == 9 and seen["idle"] == 120.0 and seen["bring"]
    assert "pressed cmd+ctrl+left" in capsys.readouterr().out


#: the window server's list with Gatekeeper's alert in front, which yabai does not manage: its tiny
#: helper window first, then the alert itself, then the window behind it
DIALOG_FRONT = [
    {"kCGWindowOwnerName": "CoreServicesUIAgent", "kCGWindowOwnerPID": 58901, "kCGWindowNumber": 14370,
     "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1, "Height": 1}},
    {"kCGWindowOwnerName": "CoreServicesUIAgent", "kCGWindowOwnerPID": 58901, "kCGWindowNumber": 14369,
     "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 766, "Y": 236, "Width": 260, "Height": 274}},
    {"kCGWindowOwnerName": "Notes", "kCGWindowOwnerPID": 300, "kCGWindowNumber": 7,
     "kCGWindowAlpha": 1.0, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 900, "Height": 700}},
]


class Unmanaged(Mac):
    """yabai has no answer for the front window; LaunchServices names the app in front."""

    def __init__(self, front_pid="58901", **kw):
        super().__init__(**kw)
        self.front_pid = front_pid

    def run(self, cmd, **k):
        if cmd[1:] == ["-m", "query", "--windows", "--window"]:
            class R:
                returncode, stdout = 1, "could not retrieve window details."
            self.ran.append(cmd)
            return R()
        if cmd[0] == "/usr/bin/lsappinfo":
            class L:
                returncode, stdout = 0, ""
            self.ran.append(cmd)
            if self.front_pid is None:
                L.returncode = 1
            else:
                L.stdout = "ASN:0x0-0x5e05e:\n" if cmd[1] == "front" else f'"pid"={self.front_pid}\n'
            return L()
        return super().run(cmd, **k)


def test_front_falls_back_to_the_window_server_when_yabai_has_no_answer():
    mac = Unmanaged()
    got = keys.front(run=mac.run, windows=lambda: DIALOG_FRONT)
    assert got == {"id": 14369, "app": "CoreServicesUIAgent", "pid": 58901, "title": "", "source": "window server"}
    assert keys.front(run=Unmanaged(front_pid=None).run, windows=lambda: DIALOG_FRONT) is None
    assert keys.front(run=Unmanaged(front_pid="999").run, windows=lambda: DIALOG_FRONT) is None


def test_a_key_for_a_window_yabai_does_not_manage_goes_to_the_station():
    mac = Unmanaged(front=14369)
    got = keys.press(14369, ["escape"], token="tok", run=mac.run, opener=mac.opener, sleep=mac.sleep,
                     clock=lambda: sum(mac.slept), windows=lambda: DIALOG_FRONT)
    assert got["ok"] and got["front_before"] == {"id": 14369, "app": "CoreServicesUIAgent"} and not got["brought"]
    assert mac.posted == [{"type": "key", "code": 53, "flags": 0, "window": 14369}]
    mac = Unmanaged(front_pid=None)
    got = keys.press(14369, ["escape"], token="tok", run=mac.run, opener=mac.opener, sleep=mac.sleep,
                     clock=lambda: sum(mac.slept), windows=lambda: DIALOG_FRONT)
    assert not got["ok"] and "neither yabai nor the window server" in got["why"] and not mac.posted
