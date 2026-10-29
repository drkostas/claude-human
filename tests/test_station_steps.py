"""The step session without a station, a screen or a clock: every outside call is a stand-in."""
import json
import os

import pytest

from claude_human.station import steps


class World:
    def __init__(self, tmp, locked=(False,), windows=None, pictures=None):
        self.out, self.sent, self.locked_seq, self.t = str(tmp), [], list(locked), 0.0
        self.windows = windows if windows is not None else [{"id": 7, "app": "Chat", "width": 900, "height": 700},
                                                            {"id": 8, "app": "Chat", "width": 200, "height": 100}]
        self.pictures = list(pictures or [b"a"])
        self.steps_to_write, self.awake, self.exited, self.opened = {}, [], [], []

    def send(self, msg):
        self.sent.append(msg)
        return {"ok": True}

    def shot(self, wid):
        pic = self.pictures.pop(0) if len(self.pictures) > 1 else self.pictures[0]
        return (pic, 10, 10)

    def locked(self):
        return self.locked_seq.pop(0) if len(self.locked_seq) > 1 else self.locked_seq[0]

    def sleep(self, s):
        self.t += max(s, 0.1)
        for at, (n, cmd) in list(self.steps_to_write.items()):
            if self.t >= at:
                with open(os.path.join(self.out, f"cmd-{n}.json"), "w") as f:
                    json.dump(cmd, f)
                del self.steps_to_write[at]

    def clock(self):
        return self.t

    def session(self, **kw):
        class Proc:
            def __init__(p, world):
                p.world = world

            def terminate(p):
                p.world.awake.append("stopped")
        kw.setdefault("app", "Chat")
        return steps.StepSession(self.out, token="tok", send=self.send, shot=self.shot,
                                 windows=lambda: self.windows, locked=self.locked,
                                 keep_awake=lambda s: self.awake.append(s) or Proc(self),
                                 lines=lambda path: [{"text": os.path.basename(path), "mid": [0.5, 0.5]}],
                                 opener=self.opened.append, on_exit=lambda: self.exited.append(1),
                                 sleep=self.sleep, clock=self.clock, log=lambda m: None, **kw)


def done(world, n):
    with open(os.path.join(world.out, f"done-{n}.json")) as f:
        return json.load(f)


def test_it_waits_for_the_unlock_keeps_awake_and_runs_steps_in_order(tmp_path):
    w = World(tmp_path, locked=(True, True, False))
    w.steps_to_write = {1.0: (1, {"click": [0.2, 0.3]}), 2.0: (2, {"text": "Brazil"}),
                        3.0: (3, {"scroll": {"dy": -3, "unit": "line"}}), 4.0: (4, {"stop": True})}
    assert w.session(open_app=True, total=600).run() == 0
    assert w.opened == ["Chat"] and w.awake[0] == 600 and w.awake[-1] == "stopped" and w.exited == [1]
    ready = json.load(open(os.path.join(w.out, "ready.json")))
    assert ready["ok"] and ready["window"] == 7                     # the app's largest window
    kinds = [m["type"] for m in w.sent]
    assert kinds == ["raise", "raise", "click", "raise", "text", "raise", "scroll"]
    assert all(m["window"] == 7 for m in w.sent)
    assert w.sent[-1] == {"type": "scroll", "dy": -3, "dx": 0, "x": 0.5, "y": 0.5, "unit": "line", "window": 7}
    assert done(w, 1)["pages"][0]["lines"][0]["text"] == "step-1.jpg"
    assert os.path.exists(os.path.join(w.out, "step-2.jpg"))


def test_paging_stops_when_the_picture_stops_changing(tmp_path):
    w = World(tmp_path, pictures=[b"r", b"p0", b"p1", b"p2", b"p2"])
    w.steps_to_write = {1.0: (1, {"page": {"dy": 600, "max": 9, "phased": True}}), 2.0: (2, {"stop": True})}
    assert w.session(window=7, app=None).run() == 0
    d = done(w, 1)
    assert [p["name"] for p in d["pages"]] == ["step-1-p000", "step-1-p001", "step-1-p002"]
    assert d["end"] == "the picture stopped changing"
    assert not os.path.exists(os.path.join(w.out, "step-1-p003.jpg"))
    assert [m.get("phased") for m in w.sent if m["type"] == "scroll"] == [True, True, True]


def test_no_step_in_time_a_locked_session_or_no_window_end_it_and_still_clean_up(tmp_path):
    w = World(tmp_path)
    assert w.session(step_wait=5).run() == 0 and w.exited == [1] and w.awake[-1] == "stopped"
    w = World(tmp_path / "locked", locked=(True,))
    assert w.session(wait_unlock=3).run() == 2 and w.awake == [] and w.exited == []
    w = World(tmp_path / "nowin", windows=[])
    assert w.session().run() == 3 and w.exited == [1]
    w = World(tmp_path / "relocked", locked=(False, True))
    w.steps_to_write = {1.0: (1, {"click": [0.5, 0.5]})}
    assert w.session().run() == 4 and done(w, 1)["locked"] and not any(m["type"] == "click" for m in w.sent)


def test_a_keepalive_does_nothing_and_a_session_needs_a_window_or_an_app(tmp_path):
    w = World(tmp_path)
    w.steps_to_write = {1.0: (1, {"noop": True}), 2.0: (2, {"stop": True})}
    assert w.session().run() == 0 and done(w, 1) == {"cmd": {"noop": True}}
    assert [m["type"] for m in w.sent] == ["raise"]
    with pytest.raises(ValueError):
        steps.StepSession(str(tmp_path), token="t")


def test_the_client_lists_windows_and_refusals_are_none():
    import io
    import urllib.error
    from claude_human.station import client

    def ok(req, timeout=None):
        assert req.full_url.endswith("/windows") and req.get_header("Authorization") == "Bearer t"
        return io.BytesIO(json.dumps({"windows": [{"id": 1}]}).encode())

    def no(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)
    assert client.windows(token="t", opener=ok) == [{"id": 1}]
    assert client.windows(token="t", opener=no) is None
