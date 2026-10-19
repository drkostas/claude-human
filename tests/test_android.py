"""android.tap taps only when the app's own window has focus."""
from __future__ import annotations

from claude_human import android

PKG = "com.example.app"
APP_FOCUS = f"  mCurrentFocus=Window{{1 u0 {PKG}/{PKG}.MainActivity}}"
SHADE = "  mCurrentFocus=Window{2 u0 NotificationShade}"


class Fake:
    def __init__(self, focuses, locked=False, launcher=f"{PKG}/.MainActivity"):
        self.focuses, self.locked, self.launcher, self.calls = list(focuses), locked, launcher, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[2:] == ["shell", "dumpsys", "window"]:
            f = self.focuses[0] if len(self.focuses) == 1 else self.focuses.pop(0)
            return 0, f"{f}\n  isKeyguardShowing={'true' if self.locked else 'false'}\n"
        if "resolve-activity" in argv:
            return 0, f"priority=0\n{self.launcher}\n" if self.launcher else "No activity found\n"
        return 0, ""

    def taps(self):
        return [c for c in self.calls if "tap" in c]

    def starts(self):
        return [c for c in self.calls if "start" in c]


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
