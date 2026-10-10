"""QuartzInput's logic, with a stand-in for Quartz, so it runs on any platform."""
from types import SimpleNamespace

from claude_human.station.input import QuartzInput


class FakeQuartz:
    kCGHIDEventTap = 0
    kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventLeftMouseDragged = "ldown", "lup", "ldrag"
    kCGEventRightMouseDown, kCGEventRightMouseUp, kCGEventRightMouseDragged = "rdown", "rup", "rdrag"
    kCGMouseButtonLeft, kCGMouseButtonRight = 0, 1
    kCGEventMouseMoved = "move"
    kCGMouseEventClickState = "clickstate"
    kCGScrollEventUnitPixel = "px"
    kCGEventFlagMaskCommand = 0x100000

    def CGPointMake(self, x, y):
        return (round(x, 3), round(y, 3))

    def CGDisplayBounds(self, _d):
        return SimpleNamespace(size=SimpleNamespace(width=1000, height=500))

    def CGMainDisplayID(self):
        return 1

    def CGEventCreateMouseEvent(self, _s, kind, pt, button):
        return {"kind": kind, "pt": pt, "button": button}

    def CGEventSetIntegerValueField(self, ev, field, value):
        ev[field] = value

    def CGEventCreateScrollWheelEvent(self, _s, unit, n, dy, dx):
        return {"kind": "scroll", "dy": dy, "dx": dx}

    def CGEventCreateKeyboardEvent(self, _s, code, down):
        return {"kind": "key", "code": code, "down": down, "flags": 0}

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventSetLocation(self, ev, pt):
        ev["pt"] = pt


class Screen:
    def __init__(self, locked=False, blocker=None, apps=("Notes",), front=7):
        self.locked, self.blocker, self.apps, self.front = locked, blocker, apps, front

    def front_window(self):
        return self.front

    def screen_locked(self):
        return self.locked

    def blocked_by(self):
        return self.blocker

    def resolve(self, app, wid):
        if app:
            return 7 if app in self.apps else None
        return wid

    def window_rect(self, wid):
        o = SimpleNamespace(x=100, y=50)
        return SimpleNamespace(origin=o, size=SimpleNamespace(width=200, height=100)) if wid == 7 else None


def sink(**kw):
    posted = []
    s = QuartzInput(Screen(**kw), post=posted.append, quartz=FakeQuartz(), sleep=lambda s: None)
    return s, posted


def test_spotlight_is_command_space_whatever_the_body_says():
    s, posted = sink()
    assert s.do_input({"type": "spotlight", "code": 12, "flags": 0})["ok"]
    assert [(e["code"], e["down"], e["flags"]) for e in posted] == [(49, True, 0x100000), (49, False, 0x100000)]


def test_a_locked_mac_refuses_everything_but_a_move():
    s, posted = sink(locked=True)
    r = s.do_input({"type": "click", "x": 0.5, "y": 0.5})
    assert r["ok"] is False and r["locked"] is True and posted == []
    assert s.do_input({"type": "move", "x": 0.5, "y": 0.5})["ok"] and posted[0]["kind"] == "move"


def test_a_system_dialog_refuses_clicks():
    s, posted = sink(blocker="SecurityAgent")
    r = s.do_input({"type": "key", "code": 36})
    assert r["ok"] is False and r["blocked"] is True and posted == []


def test_a_missing_window_is_refused_and_never_mapped_to_the_display():
    s, posted = sink(apps=())
    r = s.do_input({"type": "click", "app": "Notes", "x": 0.5, "y": 0.5})
    assert r == {"ok": False, "detail": "that window is not on the visible desktop"} and posted == []


def test_points_are_normalised_inside_the_window_or_the_display():
    s, posted = sink()
    s.do_input({"type": "click", "app": "Notes", "x": 0.5, "y": 0.5})
    assert {e["pt"] for e in posted} == {(200.0, 100.0)}
    posted.clear()
    s.do_input({"type": "move", "x": 0.25, "y": 0.5})
    assert posted[0]["pt"] == (250.0, 250.0)


def test_double_click_carries_the_click_count():
    s, posted = sink()
    s.do_input({"type": "dblclick", "app": "Notes", "x": 0, "y": 0})
    assert [(e["kind"], e["clickstate"]) for e in posted] == \
        [("ldown", 1), ("lup", 1), ("ldown", 2), ("lup", 2)]


def test_right_button_and_scroll_and_unknown():
    s, posted = sink()
    s.do_input({"type": "click", "button": "right", "x": 0, "y": 0})
    assert [e["kind"] for e in posted] == ["rdown", "rup"]
    posted.clear()
    s.do_input({"type": "scroll", "dx": 3, "dy": -40})
    assert posted == [{"kind": "scroll", "dy": -40, "dx": 3}]
    assert s.do_input({"type": "launch"})["ok"] is False


def test_text_is_pasted_and_the_clipboard_is_put_back():
    class Board:
        def __init__(self):
            self.value, self.history = "before", []

        def stringForType_(self, _k):
            return self.value

        def clearContents(self):
            self.value = None

        def setString_forType_(self, v, _k):
            self.value = v
            self.history.append(v)

    board = Board()
    posted = []
    s = QuartzInput(Screen(), post=posted.append, quartz=FakeQuartz(), pasteboard=(board, "str"),
                    sleep=lambda s: None)
    assert s.do_input({"type": "text", "text": "hello"})["ok"]
    assert board.history == ["hello", "before"] and board.value == "before"
    assert [(e["code"], e["flags"]) for e in posted] == [(9, 0x100000), (9, 0x100000)]


def test_typing_into_a_named_window_needs_it_in_front():
    s, posted = sink(front=3)
    for msg in ({"type": "text", "text": "hi", "app": "Notes"}, {"type": "key", "code": 36, "app": "Notes"}):
        r = s.do_input(msg)
        assert r["ok"] is False and "not in front" in r["detail"]
    assert posted == []


def test_typing_goes_through_once_the_window_is_in_front():
    s, posted = sink(front=7)
    assert s.do_input({"type": "key", "code": 36, "app": "Notes"})["ok"]
    assert [e["code"] for e in posted] == [36, 36]


def test_a_screen_that_cannot_say_what_is_in_front_refuses_typing_into_a_window():
    class Blind(Screen):
        front_window = None
    posted = []
    s = QuartzInput(Blind(), post=posted.append, quartz=FakeQuartz(), sleep=lambda s: None)
    assert s.do_input({"type": "key", "code": 36, "app": "Notes"})["ok"] is False and posted == []


def test_scroll_in_a_named_window_lands_inside_it():
    s, posted = sink(front=3)          # scrolling needs no focus, only the right place
    assert s.do_input({"type": "scroll", "app": "Notes", "dy": -40, "x": 0.5, "y": 0.5})["ok"]
    assert posted[0] == {"kind": "move", "pt": (200.0, 100.0), "button": 0}
    assert posted[1] == {"kind": "scroll", "dy": -40, "dx": 0, "pt": (200.0, 100.0)}
