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
    kCGScrollEventUnitLine = "line"
    kCGScrollWheelEventScrollPhase = "phase"
    kCGScrollPhaseBegan, kCGScrollPhaseChanged, kCGScrollPhaseEnded = 1, 2, 4
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
        ev = {"kind": "scroll", "dy": dy, "dx": dx}
        if unit != "px":
            ev["unit"] = unit
        return ev

    def CGEventCreateKeyboardEvent(self, _s, code, down):
        return {"kind": "key", "code": code, "down": down, "flags": 0}

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventSetLocation(self, ev, pt):
        ev["pt"] = pt


class Screen:
    def __init__(self, locked=False, blocker=None, apps=("Notes",), front=7, on_top=None):
        self.locked, self.blocker, self.apps, self.front = locked, blocker, apps, front
        self.on_top, self.asked_at = on_top, []

    def on_top_at(self, wid, x, y):
        self.asked_at.append((wid, x, y))
        return self.on_top

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


def test_a_dialog_refusal_names_the_dialog_and_can_be_worded_from_it():
    s, posted = sink(blocker="UserNotificationCenter")
    r = s.do_input({"type": "click", "x": 0.5, "y": 0.5})
    assert r["blocker"] == "UserNotificationCenter" and r["detail"] == s.blocked_detail and posted == []
    worded = QuartzInput(Screen(blocker="UserNotificationCenter"), post=posted.append, quartz=FakeQuartz(),
                         blocked_detail=lambda app: f"{app} is asking something")
    assert worded.do_input({"type": "click", "x": 0.5, "y": 0.5})["detail"] == "UserNotificationCenter is asking something"
    assert worded.do_input({"type": "move", "x": 0.5, "y": 0.5})["ok"], "a move is still allowed"


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


class Consent:
    """claude_human.consent as the station uses it: the prompts on screen and what answer pressed."""
    AX_OWNERS = {"CoreServicesUIAgent"}

    def __init__(self, found):
        self.found, self.answered = found, []

    def prompts(self):
        return self.found

    def answer(self, choice, find, by_title=False, wid=None):
        self.answered.append((choice, by_title) if wid is None else (choice, by_title, wid))
        return True, "Pressed Cancel."

    def read_words(self, pid, wid):
        return '"tool" wants access to control "Browser".' 


GATE = {"wid": 14369, "pid": 58901, "owner": "CoreServicesUIAgent"}


def test_a_consent_answer_reaches_the_dialog_that_blocks_every_other_input():
    consent = Consent([GATE])
    s = QuartzInput(Screen(blocker="CoreServicesUIAgent"), post=[].append, quartz=FakeQuartz(),
                    sleep=lambda s: None, consent=consent)
    assert s.do_input({"type": "click", "x": 0.5, "y": 0.5})["blocked"]
    got = s.do_input({"type": "consent", "choice": "deny"})
    assert got == {"ok": True, "detail": "Pressed Cancel.", "owner": "CoreServicesUIAgent", "window": 14369}
    assert consent.answered == [("deny", True)]   # pressed by its button's title


def test_a_consent_answer_is_refused_locked_or_unknown_and_any_owners_prompt_is_pressed_by_title():
    consent = Consent([GATE])
    s = QuartzInput(Screen(locked=True), quartz=FakeQuartz(), consent=consent)
    assert s.do_input({"type": "consent", "choice": "allow"})["locked"] and consent.answered == []
    s = QuartzInput(Screen(), quartz=FakeQuartz(), consent=consent)
    assert not s.do_input({"type": "consent", "choice": "open it"})["ok"] and consent.answered == []
    consent = Consent([{"wid": 5, "pid": 42, "owner": "UserNotificationCenter", "allow": "Allow", "deny": "Don't Allow"}])
    s = QuartzInput(Screen(blocker="UserNotificationCenter"), quartz=FakeQuartz(), consent=consent)
    assert s.do_input({"type": "consent", "choice": "allow"})["ok"] and consent.answered == [("allow", True)]
    got = s.do_input({"type": "consent", "choice": "read"})
    assert got["ok"] and got["words"] == '"tool" wants access to control "Browser".' and got["allow"] == "Allow"
    assert consent.answered == [("allow", True)]                     # reading presses nothing
    for found, words in (([], "No consent prompt"), (None, "cannot be read")):
        s = QuartzInput(Screen(), quartz=FakeQuartz(), consent=Consent(found))
        assert words in s.do_input({"type": "consent", "choice": "deny"})["detail"]


def test_a_click_on_a_window_covered_at_that_point_is_refused_and_says_what_is_on_top():
    screen = Screen(on_top="iTerm2")
    posted = []
    s = QuartzInput(screen, post=posted.append, quartz=FakeQuartz(), sleep=lambda s: None)
    got = s.do_input({"type": "click", "app": "Notes", "x": 0.5, "y": 0.5})
    assert not got["ok"] and got["covered"] and got["on_top"] == "iTerm2" and "iTerm2" in got["detail"]
    assert posted == [] and screen.asked_at == [(7, 200.0, 100.0)]
    for kind in ("down", "dblclick"):
        assert not s.do_input({"type": kind, "app": "Notes", "x": 0.5, "y": 0.5})["ok"]
    assert posted == []


def test_a_release_or_a_move_is_never_refused_for_cover():
    s = QuartzInput(Screen(on_top="iTerm2"), post=(posted := []).append, quartz=FakeQuartz(), sleep=lambda s: None)
    for kind in ("up", "drag", "move"):
        assert s.do_input({"type": kind, "app": "Notes", "x": 0.5, "y": 0.5})["ok"], kind
    assert len(posted) == 3


def test_a_click_on_the_whole_display_or_an_unreadable_list():
    s = QuartzInput(Screen(on_top="iTerm2"), post=(posted := []).append, quartz=FakeQuartz(), sleep=lambda s: None)
    assert s.do_input({"type": "click", "x": 0.5, "y": 0.5})["ok"]   # no window named: the point is the point

    class Unreadable(Screen):
        def on_top_at(self, wid, x, y):
            raise RuntimeError("no window list")
    s = QuartzInput(Unreadable(), post=(posted := []).append, quartz=FakeQuartz(), sleep=lambda s: None)
    got = s.do_input({"type": "click", "app": "Notes", "x": 0.5, "y": 0.5})
    assert not got["ok"] and "could not be read" in got["detail"] and posted == []


def test_scroll_in_lines_or_with_phases_for_apps_that_ignore_plain_pixel_deltas():
    s, posted = sink(front=3)
    assert s.do_input({"type": "scroll", "app": "Notes", "dy": -3, "unit": "line"})["ok"]
    assert posted[1] == {"kind": "scroll", "dy": -3, "dx": 0, "unit": "line", "pt": (200.0, 100.0)}
    s, posted = sink(front=3)
    assert s.do_input({"type": "scroll", "app": "Notes", "dy": -60, "phased": True})["ok"]
    scrolls = [e for e in posted if e["kind"] == "scroll"]
    assert [(e["dy"], e.get("phase")) for e in scrolls] == [(0, 1), (-60, 2), (0, 4)]
    assert all(e["pt"] == (200.0, 100.0) for e in scrolls)
    s, posted = sink(front=3)
    s.do_input({"type": "scroll", "app": "Notes", "dy": -60})
    assert [e.get("phase") for e in posted if e["kind"] == "scroll"] == [None]   # the default is unchanged


def test_raise_brings_the_named_window_forward_and_observes_it():
    screen, asked = Screen(front=9), []

    def focus(wid):
        asked.append(wid)
        screen.front = wid
        return True
    s = QuartzInput(screen, post=[].append, quartz=FakeQuartz(), sleep=lambda t: None, focus=focus)
    assert s.do_input({"type": "raise", "app": "Notes"}) == {"ok": True, "detail": "brought to the front"}
    assert asked == [7]
    assert s.do_input({"type": "raise", "app": "Notes"})["detail"] == "it was in front already"
    stuck = QuartzInput(Screen(front=9), post=[].append, quartz=FakeQuartz(), sleep=lambda t: None,
                        focus=lambda wid: True)
    got = stuck.do_input({"type": "raise", "app": "Notes"})
    assert not got["ok"] and "did not come" in got["detail"]
    assert not s.do_input({"type": "raise"})["ok"]                   # no window named
    assert QuartzInput(Screen(locked=True), quartz=FakeQuartz(), focus=focus).do_input(
        {"type": "raise", "app": "Notes"})["locked"]


def test_a_consent_answer_can_name_the_prompts_window():
    consent = Consent([GATE, {"wid": 5, "pid": 42, "owner": "UserNotificationCenter"}])
    s = QuartzInput(Screen(), quartz=FakeQuartz(), consent=consent)
    got = s.do_input({"type": "consent", "choice": "allow", "prompt": 5})
    assert got["ok"] and got["window"] == 5 and consent.answered == [("allow", True, 5)]
    got = s.do_input({"type": "consent", "choice": "allow", "prompt": 77})
    assert not got["ok"] and "no longer open" in got["detail"] and len(consent.answered) == 1
