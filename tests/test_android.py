"""android.tap taps only when the tap can land where it is meant to, and reads the screen back."""
from __future__ import annotations

import json

from claude_human import android

PKG = "com.example.app"
APP_FOCUS = f"  mCurrentFocus=Window{{1 u0 {PKG}/{PKG}.MainActivity}}"
SHADE = "  mCurrentFocus=Window{2 u0 NotificationShade}"
#: SystemUI's heads-up lines with no banner, as an Android 16 phone printed them (filtered by SYSTEMUI)
QUIET_UI = """      mHeadsUpPinnedMode=false
      mHeadsUpAnimatingAway=false
      HeadsUpManager state:
        mTouchableRegion=SkRegion()
        mTouchableRegion=SkRegion((0,0,1272,141)(597,141,675,177))
"""
#: dumpsys power's lines when the last input was four minutes ago, as printed on the same phone
IDLE_POWER = """  mLastWakeTime=77850373 (1740809 ms ago)
  mLastInteractivePowerHintTime=79358224 (232958 ms ago)
 mLastUserActivityTime(excludingAttention)=79358224
"""
JUST_USED = """  mLastInteractivePowerHintTime=79728323 (503 ms ago)
 mLastUserActivityTime(excludingAttention)=79728323
"""
FRAME = "        1272        2772           1           1\n14103952\n"


def dump(*items: str) -> str:
    nodes = "".join(f'<node text="{t}" content-desc="" />' for t in items)
    return f"<?xml version='1.0' ?><hierarchy rotation=\"0\">{nodes}</hierarchy>"


class Fake:
    """adb as the tap sees it: each read answered by what it asks for. A list answers once per call
    in turn and then keeps its last answer."""

    def __init__(self, focuses, locked=False, launcher=f"{PKG}/.MainActivity", *, systemui=QUIET_UI,
                 telephony="    mCallState=0\n    mCallState=0\n", audio="- Actual mode = MODE_NORMAL\n",
                 power=IDLE_POWER, bands=("a" * 32,), dumps=(dump("Inbox"),)):
        self.focuses, self.locked, self.launcher, self.calls = list(focuses), locked, launcher, []
        self.answers = {android.SYSTEMUI: _seq(systemui), android.TELEPHONY: _seq(telephony),
                        android.AUDIO: _seq(audio), android.POWER: _seq(power), android.FRAME: _seq(FRAME),
                        android.DUMP: _seq(dumps)}
        self.bands = _seq(bands)

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[2:] == ["shell", "dumpsys", "window"]:
            f = self.focuses[0] if len(self.focuses) == 1 else self.focuses.pop(0)
            return 0, f"{f}\n  isKeyguardShowing={'true' if self.locked else 'false'}\n"
        if "resolve-activity" in argv:
            return 0, f"priority=0\n{self.launcher}\n" if self.launcher else "No activity found\n"
        if len(argv) == 4 and argv[2] == "shell":
            if argv[3] in self.answers:
                return 0, self.answers[argv[3]]()
            if argv[3].startswith("screencap | tail"):
                return 0, f"{self.bands()}  -\n"
        return 0, ""

    def taps(self):
        return [c for c in self.calls if "tap" in c]

    def starts(self):
        return [c for c in self.calls if "start" in c]


def _seq(value):
    items = list(value) if isinstance(value, (list, tuple)) else [value]

    def nxt():
        return items.pop(0) if len(items) > 1 else items[0]
    return nxt


def tap(f, **kw):
    kw.setdefault("sleep", lambda s: None)
    return android.checked_tap("dev", PKG, 10, 20, run=f, **kw)


def test_taps_when_the_app_has_focus():
    f = Fake([APP_FOCUS])
    assert android.tap("dev", PKG, 10, 20, run=f, sleep=lambda s: None)[0] == "success"
    assert f.taps() == [["-s", "dev", "shell", "input", "tap", "10", "20"]] and not f.starts()


def test_a_shade_over_the_app_is_not_focus():
    f = Fake([SHADE])
    outcome, why = android.tap("dev", PKG, 10, 20, run=f, wait=0.01, sleep=lambda s: None)
    assert outcome == "refused" and "NotificationShade" in why and not f.taps()


def test_brings_the_app_forward_with_am_start_never_monkey():
    f = Fake([SHADE, SHADE, APP_FOCUS])
    assert android.tap("dev", PKG, 1, 2, run=f, sleep=lambda s: None)[0] == "success"
    assert f.starts() == [["-s", "dev", "shell", "am", "start", "-n", f"{PKG}/.MainActivity"]]
    assert not any("monkey" in c for c in f.calls) and len(f.taps()) == 1


def test_a_locked_phone_or_no_launcher_is_refused():
    f = Fake([APP_FOCUS], locked=True)
    assert android.tap("dev", PKG, 1, 2, run=f) == ("refused", "the phone is locked") and not f.taps()
    f = Fake([SHADE], launcher="")
    assert android.tap("dev", PKG, 1, 2, run=f)[0] == "refused" and not f.taps()


def test_a_heads_up_banner_is_refused_though_the_app_has_focus():
    # a banner takes no focus: mCurrentFocus still names the app while one shows
    for ui, words in ((QUIET_UI.replace("mHeadsUpPinnedMode=false", "mHeadsUpPinnedMode=true"), "pinned"),
                      (QUIET_UI.replace("mHeadsUpAnimatingAway=false", "mHeadsUpAnimatingAway=true"), "leaving"),
                      (QUIET_UI + "      isHeadsUpState()=true isPinned()=true\n", "heads-up"),
                      (QUIET_UI.replace("SkRegion()", "SkRegion((0,0,1272,420))", 1), "takes touches")):
        f = Fake([APP_FOCUS], systemui=ui)
        r = tap(f, wait=0)
        assert r["outcome"] == "refused" and words in r["why"] and not f.taps(), (words, r)


def test_the_heads_up_reading_of_a_quiet_screen():
    assert android.heads_up(QUIET_UI) is None
    assert android.heads_up("") is None


def test_a_call_is_refused_by_its_call_state_or_its_audio_mode():
    f = Fake([APP_FOCUS], telephony="    mCallState=0\n    mCallState=2\n")
    r = tap(f, wait=0)
    assert r["outcome"] == "refused" and "call state 2" in r["why"] and not f.taps()
    f = Fake([APP_FOCUS], audio="- Requested mode = MODE_IN_COMMUNICATION\n- Actual mode = MODE_IN_COMMUNICATION\n")
    r = tap(f, wait=0)   # a call an app makes over the internet leaves the SIMs' state at 0
    assert r["outcome"] == "refused" and "MODE_IN_COMMUNICATION" in r["why"] and not f.taps()


def test_the_phone_in_use_is_waited_for_then_refused():
    assert android.idle_s(IDLE_POWER) == 232.958 and android.idle_s(JUST_USED) == 0.503
    assert android.idle_s("") is None
    f = Fake([APP_FOCUS], power=JUST_USED)
    r = tap(f, wait=0)
    assert r["outcome"] == "refused" and "0.5 s ago" in r["why"] and not f.taps()
    f = Fake([APP_FOCUS], power=[JUST_USED, IDLE_POWER])
    clock = iter(range(100))
    assert tap(f, wait=5, clock=lambda: next(clock))["outcome"] == "success" and len(f.taps()) == 1


def test_a_screen_still_moving_around_the_point_is_refused():
    f = Fake([APP_FOCUS], bands=("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    r = tap(f, wait=0)
    assert r["outcome"] == "refused" and "still moving" in r["why"] and not f.taps()
    hashed = [c[3] for c in f.calls if len(c) == 4 and c[3].startswith("screencap | tail")]
    assert hashed[0] == f"screencap | tail -c +{16 + 0 * 1272 * 4 + 1} | head -c {140 * 1272 * 4} | md5sum"


def test_the_frame_header_is_measured_not_assumed():
    assert android.frame("dev", Fake([APP_FOCUS])) == (1272, 2772, 4, 16)
    f = Fake([APP_FOCUS])
    f.answers[android.FRAME] = lambda: "  100  50  1  4278190080\n20012\n"   # a 12-byte header
    assert android.frame("dev", f) == (100, 50, 4, 12)


def test_the_screen_is_read_back_and_what_changed_is_named():
    after = "  mCurrentFocus=Window{3 u0 com.example.app/com.example.app.DetailActivity}"
    f = Fake([APP_FOCUS, APP_FOCUS, APP_FOCUS, APP_FOCUS, APP_FOCUS, after],
             dumps=(dump("Inbox", "Archive"), dump("Inbox", "Message from a friend")))
    r = tap(f)
    assert r["outcome"] == "success" and r["appeared"] == ["Message from a friend"] and r["gone"] == ["Archive"]
    assert "DetailActivity" in r["focus_after"]
    line = android.summary(r)
    assert "1 text(s) appeared, 1 went" in line and "focus is now" in line


def test_a_dump_that_fails_is_reported_as_not_read_back():
    f = Fake([APP_FOCUS], dumps=("ERROR: could not get idle state.\n",))
    r = tap(f)
    assert r["outcome"] == "success" and r["appeared"] is None
    assert "not read back" in android.summary(r)


def test_long_texts_are_cut_and_the_json_report_is_whole():
    long = "x" * 200
    f = Fake([APP_FOCUS], dumps=(dump("a"), dump("a", long)))
    r = tap(f)
    assert r["appeared"] == ["x" * (android.TEXT_MAX - 1) + "…"]
    assert json.loads(json.dumps(r, ensure_ascii=False))["outcome"] == "success"
