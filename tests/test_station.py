"""The station server, run on a free port with a fake screen and a fake input sink."""
import http.client
import json
import os
import stat
import threading

import pytest

from claude_human.station import Grant, StationAuth, TokenAuth, Unavailable, make_server
from claude_human.station.auth import load_or_create_token
from claude_human.station.view import render

JPEG = b"\xff\xd8fakejpeg\xff\xd9"


class FakeScreen:
    def __init__(self):
        self.wins = [{"id": 11, "app": "Notes", "title": "a", "x": 0, "y": 0, "width": 800, "height": 600},
                     {"id": 22, "app": "Safari", "title": "b", "x": 0, "y": 0, "width": 900, "height": 700}]
        self.framed = []
        self.locked = False

    def windows(self):
        return list(self.wins)

    def resolve(self, app, wid):
        if app:
            m = [w for w in self.wins if w["app"].lower() == app.lower()]
            return m[0]["id"] if m else None
        return wid

    def frame(self, wid):
        self.framed.append(wid)
        return (JPEG + str(wid).encode(), 100, 50)

    def screen_locked(self):
        return self.locked


class FakeInput:
    def __init__(self):
        self.got = []

    def do_input(self, msg):
        self.got.append(dict(msg))
        return {"ok": True, "echo": msg.get("type")}

    def can_act(self):
        return True


class FakeAuth(StationAuth):
    """master-token is a master grant, pinned-* are pinned grants for the app after the dash."""

    def __init__(self):
        self.performed = []
        self.inputs = []
        self.app_error = None

    def check(self, secret):
        if secret == "master-token":
            return Grant(master=True)
        if secret == "boom":
            raise RuntimeError("database down")
        if secret.startswith("pinned-"):
            return Grant(master=False, station=secret[7:], holder="person:a")
        return None

    def app_for(self, grant):
        if self.app_error:
            raise self.app_error
        return None if grant.station == "display" else grant.station

    def perform(self, grant, act, run):
        self.performed.append((grant.holder, act))
        return run()

    def on_input(self, grant, msg, result):
        self.inputs.append((grant.master, msg.get("type"), result.get("ok")))


@pytest.fixture()
def station():
    screen, sink, auth = FakeScreen(), FakeInput(), FakeAuth()
    logs = []
    srv = make_server(auth, port=0, screen=screen, input=sink, log=logs.append)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], screen, sink, auth, logs
    srv.shutdown()
    srv.server_close()


def req(port, method, path, token=None, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


# ----------------------------------------------------------------------------- binding

def test_binds_to_loopback_only():
    srv = make_server(FakeAuth(), port=0, screen=FakeScreen(), input=FakeInput())
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()
    for host in ("0.0.0.0", "192.0.2.1", "", "::"):
        with pytest.raises(ValueError):
            make_server(FakeAuth(), host=host, port=0, screen=FakeScreen(), input=FakeInput())


def test_auth_must_be_a_station_auth():
    with pytest.raises(TypeError):
        make_server(object(), port=0, screen=FakeScreen(), input=FakeInput())


# ----------------------------------------------------------------------------- auth

def test_view_is_static_and_never_cached(station):
    port = station[0]
    code, headers, body = req(port, "GET", "/view")
    assert code == 200
    assert "no-store" in headers["Cache-Control"]
    assert b"master-token" not in body and b"__stationGrant" in body


def test_no_token_wrong_token_and_query_token_are_refused(station):
    port, screen, _sink, _auth, logs = station
    assert req(port, "GET", "/shot")[0] == 401
    assert req(port, "GET", "/shot", token="nope")[0] == 401
    # a token in the URL is not a credential
    assert req(port, "GET", "/shot?token=master-token")[0] == 401
    assert req(port, "GET", "/shot", headers={"Authorization": "master-token"})[0] == 401
    code, _h, body = req(port, "GET", "/shot", token="nope")
    assert json.loads(body) == {"error": "unauthorized"}
    assert screen.framed == []
    assert logs and all(line.startswith("401 GET /shot") for line in logs)
    assert not any("master-token" in line for line in logs)


def test_an_auth_that_raises_grants_nothing(station):
    port, screen, _s, _a, logs = station
    assert req(port, "GET", "/shot", token="boom")[0] == 401
    assert "database down" in logs[-1]
    assert screen.framed == []


def test_an_unreadable_app_is_refused_not_widened(station):
    port, screen, _s, auth, _l = station
    auth.app_error = Unavailable("no answer")
    assert req(port, "GET", "/shot", token="pinned-Notes")[0] == 401
    assert req(port, "POST", "/input", token="pinned-Notes", body={"type": "click"})[0] == 401
    assert screen.framed == []


# ----------------------------------------------------------------------------- frames

def test_master_frames_any_window(station):
    port, screen, *_ = station
    code, headers, body = req(port, "GET", "/shot?app=Safari", token="master-token")
    assert code == 200 and headers["Content-Type"] == "image/jpeg" and body == JPEG + b"22"
    assert headers["X-Frame-Size"] == "100x50"
    assert req(port, "GET", "/shot?window=11", token="master-token")[2] == JPEG + b"11"
    assert req(port, "GET", "/shot", token="master-token")[2] == JPEG + b"None"


def test_a_pinned_grant_gets_its_own_window_whatever_it_asks(station):
    port, screen, *_ = station
    assert req(port, "GET", "/shot?app=Safari&window=22", token="pinned-Notes")[2] == JPEG + b"11"
    assert screen.framed == [11]


def test_a_pinned_app_that_is_not_on_screen_is_404_never_the_display(station):
    port, screen, *_ = station
    assert req(port, "GET", "/shot", token="pinned-Mail")[0] == 404
    assert screen.framed == []


def test_the_display_station_is_a_deliberate_answer(station):
    port, screen, *_ = station
    assert req(port, "GET", "/shot", token="pinned-display")[2] == JPEG + b"None"


def test_windows_list_is_for_master_only(station):
    port, *_ = station
    code, _h, body = req(port, "GET", "/windows", token="master-token")
    assert code == 200 and [w["id"] for w in json.loads(body)["windows"]] == [11, 22]
    assert req(port, "GET", "/windows", token="pinned-Notes")[0] == 403


def test_health(station):
    port, screen, *_ = station
    screen.locked = True
    assert json.loads(req(port, "GET", "/health", token="master-token")[2]) == \
        {"ok": True, "locked": True, "trusted": True, "windows": 2}
    assert json.loads(req(port, "GET", "/health", token="pinned-Notes")[2])["windows"] is None


def _read_stream(port, path, token, parts):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Authorization": "Bearer " + token})
    r = c.getresponse()
    data = b""
    while data.count(b"Content-Length:") < parts:
        chunk = r.read1(65536) if hasattr(r, "read1") else r.read(1)
        if not chunk:
            break
        data += chunk
    c.close()
    return r.status, r.getheader("Content-Type"), data


def test_stream_sends_frames_as_parts(station):
    port, screen, *_ = station
    status, ctype, data = _read_stream(port, "/stream?fps=20", "pinned-Notes", 2)
    assert status == 200 and ctype.startswith("multipart/x-mixed-replace")
    assert data.count(b"--stationframe\r\nContent-Type: image/jpeg\r\n") >= 2
    assert (JPEG + b"11") in data


def test_stream_of_an_app_that_is_gone_ends_without_a_frame(station):
    port, screen, *_ = station
    status, _c, data = _read_stream(port, "/stream", "pinned-Mail", 1)
    assert status == 200 and b"Content-Length" not in data
    assert screen.framed == []


# ----------------------------------------------------------------------------- input

def test_master_input_is_forwarded_as_sent(station):
    port, _screen, sink, auth, _l = station
    code, _h, body = req(port, "POST", "/input", token="master-token",
                         body={"type": "click", "x": 0.5, "y": 0.25, "app": "Safari", "window": 22})
    assert code == 200 and json.loads(body)["ok"] is True
    assert sink.got == [{"type": "click", "x": 0.5, "y": 0.25, "app": "Safari", "window": 22}]
    assert auth.inputs == [(True, "click", True)]


def test_pinned_input_goes_to_the_granted_app_only(station):
    port, _screen, sink, _a, _l = station
    req(port, "POST", "/input", token="pinned-Notes",
        body={"type": "click", "x": 0.1, "y": 0.2, "app": "Terminal", "window": 22})
    assert sink.got == [{"type": "click", "x": 0.1, "y": 0.2, "app": "Notes"}]


def test_spotlight_goes_through_perform(station):
    port, _screen, sink, auth, _l = station
    code, _h, body = req(port, "POST", "/input", token="pinned-Notes", body={"type": "spotlight"})
    assert code == 200 and json.loads(body)["ok"] is True
    assert auth.performed == [("person:a", "spotlight")]
    assert sink.got[0]["type"] == "spotlight"


def test_a_refused_perform_presses_nothing(station):
    port, _screen, sink, auth, _l = station
    auth.perform = lambda grant, act, run: {"ok": False, "detail": "no"}
    assert json.loads(req(port, "POST", "/input", token="pinned-Notes",
                          body={"type": "spotlight"})[2]) == {"ok": False, "detail": "no"}
    assert sink.got == []


def test_bad_input_bodies(station):
    port, _screen, sink, *_ = station
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/input", body=b"not json",
              headers={"Authorization": "Bearer master-token", "Content-Length": "8"})
    assert c.getresponse().status == 400
    c.close()
    assert req(port, "POST", "/input", token="master-token", body=[1, 2])[0] == 400
    assert req(port, "POST", "/other", token="master-token", body={})[0] == 404
    assert sink.got == []


def test_a_failing_hook_does_not_undo_the_input(station):
    port, _screen, sink, auth, logs = station

    def bad(*a):
        raise RuntimeError("hook down")
    auth.on_input = bad
    assert json.loads(req(port, "POST", "/input", token="master-token", body={"type": "move"})[2])["ok"]
    assert sink.got and "hook down" in logs[-1]


# ----------------------------------------------------------------------------- TokenAuth

def test_token_auth_master_and_pinned():
    a = TokenAuth("s3cret")
    assert a.check("s3cret") == Grant(master=True)
    assert a.check("s3cre") is None and a.check("") is None and a.check("s3cret ") is None
    p = TokenAuth("s3cret", app="Notes")
    g = p.check("s3cret")
    assert g.master is False and p.app_for(g) == "Notes"
    with pytest.raises(ValueError):
        TokenAuth()


def test_token_file_is_read_at_use_and_made_private(tmp_path):
    f = tmp_path / "sub" / "token"
    tok, where = load_or_create_token(f)
    assert where == f and tok and stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert load_or_create_token(f)[0] == tok
    a = TokenAuth(token_file=f)
    assert a.check(tok)
    f.write_text("rotated\n")
    assert a.check(tok) is None and a.check("rotated")
    f.unlink()
    assert a.check("rotated") is None


def test_view_names_can_be_set_and_are_checked():
    html = render(grant_fn="__myGrant", pending_var="__myPending", message_type="my:grant",
                  offline="It's offline", ask_token=False)
    assert "window.__myGrant=function" in html and "'my:grant'" in html
    assert "It\\'s offline" in html and "if(!false) return;" in html
    with pytest.raises(ValueError):
        render(grant_fn="x;alert(1)")
    with pytest.raises(TypeError):
        render(colour="red")
    assert "Bearer" in render() and "?token" not in render()


# ----------------------------------------------------------------------------- a pinned window

class WindowScreen(FakeScreen):
    """Two windows of one app: the case an app pin cannot tell apart."""

    def __init__(self):
        super().__init__()
        self.wins.append({"id": 33, "app": "Safari", "title": "c", "x": 0, "y": 0,
                          "width": 1200, "height": 900})

    def resolve(self, app, wid):
        if app and wid:
            return wid if any(w["id"] == wid and w["app"] == app for w in self.wins) else None
        return super().resolve(app, wid)


class WindowAuth(FakeAuth):
    """pinned-Safari is pinned to window 22 by window_for, the smaller of Safari's two."""

    def __init__(self):
        super().__init__()
        self.window = 22
        self.window_error = None

    def window_for(self, grant):
        if self.window_error:
            raise self.window_error
        return self.window


@pytest.fixture()
def window_station():
    screen, sink, auth = WindowScreen(), FakeInput(), WindowAuth()
    srv = make_server(auth, port=0, screen=screen, input=sink, log=lambda m: None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], screen, sink, auth
    srv.shutdown()
    srv.server_close()


def test_a_pinned_window_is_the_one_shown_and_driven(window_station):
    port, screen, sink, _auth = window_station
    code, _h, body = req(port, "GET", "/shot?window=33", token="pinned-Safari")
    assert code == 200 and screen.framed[-1] == 22
    code, _h, _b = req(port, "POST", "/input", token="pinned-Safari",
                       body={"type": "click", "x": 0.5, "y": 0.5, "window": 33})
    assert code == 200 and sink.got[-1]["window"] == 22 and sink.got[-1]["app"] == "Safari"


def test_a_pinned_window_that_is_gone_is_refused_never_another_of_the_app(window_station):
    port, screen, sink, auth = window_station
    auth.window = 99
    before = list(screen.framed)
    code, _h, _b = req(port, "GET", "/shot", token="pinned-Safari")
    assert code == 404 and screen.framed == before


def test_a_window_that_cannot_be_read_is_a_refusal(window_station):
    port, _screen, _sink, auth = window_station
    auth.window_error = Unavailable("database down")
    code, _h, _b = req(port, "GET", "/shot", token="pinned-Safari")
    assert code == 401


def test_without_window_for_a_pin_is_the_app_as_before(station):
    port, screen, _sink, _auth, _logs = station
    code, _h, _b = req(port, "GET", "/shot", token="pinned-Safari")
    assert code == 200 and screen.framed[-1] == 22
