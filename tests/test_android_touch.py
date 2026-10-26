"""android_touch: a pinch is written as the touch screen's own multi-touch events, only when it can
land where it is meant to, and both fingers always lift."""
from __future__ import annotations

import struct

from claude_human import android, android_touch as touch
from test_android import APP_FOCUS, PKG, SHADE, Fake

#: `getevent -pl` of an invented phone: keys, a touch pad without INPUT_PROP_DIRECT, the screen
GETEVENT = """add device 1: /dev/input/event0
  name:     "gpio-keys"
  events:
    KEY (0001): KEY_VOLUMEDOWN        KEY_VOLUMEUP
  input props:
    <none>
add device 2: /dev/input/event1
  name:     "touchpad"
  events:
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 4, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 1000, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 1000, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_POINTER
add device 3: /dev/input/event4
  name:     "touchscreen"
  events:
    KEY (0001): BTN_TOOL_FINGER       BTN_TOUCH
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_TOUCH_MAJOR    : value 0, min 0, max 255, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 4319, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 9599, fuzz 0, flat 0, resolution 0
                ABS_MT_TRACKING_ID    : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
"""
PANEL = touch.Panel("/dev/input/event4", 4319, 9599, 9, True)
SCREEN = (1080, 2400)


def events(frame: bytes) -> list[tuple[int, int, int]]:
    return [struct.unpack("<qqHHi", frame[i:i + 24])[2:] for i in range(0, len(frame), 24)]


def xs(frame: bytes) -> list[int]:
    return [v for k, c, v in events(frame) if (k, c) == (touch.EV_ABS, touch.ABS_MT_POSITION_X)]


def test_the_touch_screen_is_the_direct_multi_touch_device():
    assert touch.touchscreen(GETEVENT) == PANEL
    assert touch.touchscreen(GETEVENT.split("add device 3")[0]) is None
    assert touch.touchscreen("") is None


def test_screen_size_and_rotation_are_read():
    assert touch.screen_size("Physical size: 1080x2400\n") == (1080, 2400)
    assert touch.screen_size("Physical size: 1080x2400\nOverride size: 720x1600\n") == (720, 1600)
    assert touch.screen_size("") is None
    assert touch.rotation("mRotation=ROTATION_0") == 0 and touch.rotation("mRotation=1") == 1
    assert touch.rotation("") is None


def test_an_event_is_the_kernel_struct():
    assert len(touch.event(3, 0x35, 7)) == 24 and len(touch.event(3, 0x35, 7, wide=False)) == 16
    assert struct.unpack("<qqHHi", touch.event(3, 0x39, -1)) == (0, 0, 3, 0x39, -1)


def test_a_pinch_in_puts_two_fingers_down_brings_them_together_and_lifts_them():
    frames = touch.pinch_frames(540, 1200, "in", panel=PANEL, screen=SCREEN, span=600, steps=6)
    assert len(frames) == 7
    first, last = events(frames[0]), events(frames[-1])
    assert (touch.EV_ABS, touch.ABS_MT_TRACKING_ID, touch.TRACKING_IDS[0]) in first
    assert (touch.EV_ABS, touch.ABS_MT_TRACKING_ID, touch.TRACKING_IDS[1]) in first
    assert (touch.EV_ABS, touch.ABS_MT_SLOT, 8) in first and (touch.EV_ABS, touch.ABS_MT_SLOT, 9) in first
    assert (touch.EV_KEY, touch.BTN_TOUCH, 1) in first
    assert last.count((touch.EV_ABS, touch.ABS_MT_TRACKING_ID, -1)) == 2 and (touch.EV_KEY, touch.BTN_TOUCH, 0) in last
    assert all(events(f)[-1] == (touch.EV_SYN, touch.SYN_REPORT, 0) for f in frames)
    # only the first frame puts fingers down
    assert not any(c == touch.ABS_MT_TRACKING_ID for f in frames[1:-1] for _k, c, _v in events(f))
    spans = [abs(a - b) for a, b in (xs(f) for f in frames[:-1])]
    assert spans == sorted(spans, reverse=True) and spans[0] > 3 * spans[-1]
    # 600 pixels at 4 device units a pixel
    assert abs(spans[0] - 2400) <= 8


def test_a_pinch_out_spreads_the_fingers_and_stays_on_the_screen():
    frames = touch.pinch_frames(20, 1200, "out", panel=PANEL, screen=SCREEN, span=600, steps=6)
    spans = [abs(a - b) for a, b in (xs(f) for f in frames[:-1])]
    assert spans == sorted(spans)
    assert all(0 <= v <= PANEL.max_x for f in frames for v in xs(f))


def test_a_pinch_goes_in_or_out_only():
    try:
        touch.pinch_frames(1, 1, "sideways", panel=PANEL, screen=SCREEN)
    except ValueError as e:
        assert "in or out" in str(e)
    else:
        raise AssertionError("a pinch sideways was accepted")


def test_the_script_lifts_the_fingers_on_any_exit():
    script = touch.play_script("/dev/input/event4", 0.016)
    assert "trap 'cat lift.ev > /dev/input/event4' EXIT" in script and "sleep 0.016" in script


class PhoneFake(Fake):
    """adb for a pinch: the tap's answers, plus the touch screen, the screen size, the ABI and the
    rotation, and a push and a play that can be made to fail."""

    def __init__(self, focuses, *, rotation="mRotation=ROTATION_0", play_code=0, **kw):
        super().__init__(focuses, **kw)
        self.answers.update({touch.GETEVENT: lambda: GETEVENT, touch.WM_SIZE: lambda: "Physical size: 1080x2400\n",
                             touch.ABI: lambda: "arm64-v8a\n", touch.ROTATION: lambda: rotation})
        self.play_code, self.pushed = play_code, []

    def __call__(self, argv):
        if argv[2:3] == ["push"]:
            self.calls.append(list(argv))
            from pathlib import Path
            self.pushed = sorted(p.name for p in Path(argv[3].rstrip(".")).iterdir())
            return 0, "pushed\n"
        if argv[2:3] == ["shell"] and argv[3].startswith("sh "):
            self.calls.append(list(argv))
            return self.play_code, "" if self.play_code == 0 else "write error"
        return super().__call__(argv)

    def shell(self, start):
        return [c for c in self.calls if c[2:3] == ["shell"] and c[3].startswith(start)]


def pinch(f, **kw):
    kw.setdefault("sleep", lambda s: None)
    return touch.checked_pinch("dev", PKG, 540, 1200, "in", run=f, **kw)


def test_a_pinch_waits_for_the_tap_checks_and_plays_the_frames():
    f = PhoneFake([APP_FOCUS])
    report = pinch(f)
    assert report["outcome"] == "success" and report["device"] == "/dev/input/event4"
    assert report["frames"] == 13 and "lift.ev" in f.pushed and "play.sh" in f.pushed and "f000.ev" in f.pushed
    assert len(f.shell("sh ")) == 1 and not f.shell("cat ")


def test_slow_takes_four_times_the_steps():
    assert pinch(PhoneFake([APP_FOCUS]), slow=True)["frames"] == 49


def test_nothing_touches_the_screen_on_a_refusal():
    for f, why in ((PhoneFake([APP_FOCUS], locked=True), "locked"),
                   (PhoneFake([SHADE], launcher=None), "no launcher"),
                   (PhoneFake([APP_FOCUS], telephony="    mCallState=1\n"), "call"),
                   (PhoneFake([APP_FOCUS], rotation="mRotation=1"), "turned")):
        clock = iter(range(0, 1000, 5))
        report = pinch(f, clock=lambda: next(clock))
        assert report["outcome"] == "refused" and why in report["why"], report["why"]
        assert not f.shell("sh ") and not f.pushed


def test_a_gesture_that_fails_half_way_still_lifts_both_fingers():
    f = PhoneFake([APP_FOCUS], play_code=1)
    report = pinch(f)
    assert report["outcome"] == "refused" and "lifted" in report["why"]
    assert f.shell("cat /data/local/tmp/claude-human-pinch/lift.ev > /dev/input/event4")


def test_the_checks_are_the_taps_own():
    assert touch.android.why_not is android.why_not
