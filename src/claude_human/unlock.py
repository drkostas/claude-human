"""Type a password where macOS ignores synthetic input, and observe what happened.

The macOS lock screen and SecurityAgent password panels accept input only from real hardware, so
CGEvent keys, clicks and Screen Sharing input are dropped there. Karabiner's DriverKit virtual HID
keyboard is seen by macOS as a physical keyboard, so keys posted through it land like keys from a
plugged-in keyboard. ``vhid_type`` (built by ``claude-human build-tools``) is the small helper that
posts them, and it runs under ``sudo -n`` because the Karabiner daemon's socket is root only.

Rules every function here keeps:

* The password goes to the helper on stdin. It is never put in argv, an environment variable or a
  log, and this module does not store it.
* Nothing is typed unless the target is there: ``unlock`` returns early when the session is not
  locked, and ``approve`` and ``use_password`` refuse when no password panel is on screen.
* One typist at a time. ``unlock``, ``approve`` and ``use_password`` hold one lock across every
  process (``HID_LOCK``, a ``SessionLock``), and look at the screen again once they hold it. Two
  unlocks that overlap would otherwise both type: the second one's keys arrive after the first has
  unlocked the Mac, into whatever application is in front.
* The lock screen must be in front when the keys go out. ``unlock`` reads the frontmost application
  right before each round of typing, after the wake and the wait, and types nothing unless it is
  the login window. ``--clear`` is Cmd+A and then Backspace, which in a mail client or a chat
  selects everything and deletes it, and the password and Return would follow.
* The result is observed, never assumed. ``unlock`` watches the session's lock bit clear,
  ``approve`` watches the panel close, ``relock`` watches the lock bit set.
* A failed unlock is retried at most ``attempts`` times (two by default), because every try is a
  real password entry and repeated wrong entries make macOS add a lockout delay.

Use this only on your own Mac, with the password of the person who asked for it. See SECURITY.md.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from typing import Callable, Optional

from . import paths
from .session_lock import Busy, SessionLock

#: Hooks for the outside world, replaced by tests.
_run = subprocess.run
_popen = subprocess.Popen
_sleep = time.sleep
_now = time.time
_exists = os.path.exists

#: the bundle id in front while the screen is locked
LOGIN_WINDOW = "com.apple.loginwindow"
#: the one lock every process that types through the virtual keyboard holds while it types
HID_LOCK = os.environ.get("CLAUDE_HUMAN_HID_LOCK", "~/.local/state/claude-human/hid-keyboard.lock")
#: how long a second typist waits for the first, which can take about a minute (two tries)
HID_WAIT_S = 120.0

Result = tuple[bool, str]


def is_locked() -> Optional[bool]:
    """Whether the console session is locked. None when it cannot be read (no Quartz, or a process
    outside the login session, which gets no session dictionary). Same reader as
    ``screenshot.screen_locked``."""
    from .screenshot import screen_locked
    return screen_locked()


def _default_prompt() -> Optional[dict]:
    from .screenshot import auth_prompt
    return auth_prompt()


def front_app(*, env: Mapping[str, str] | None = None) -> Optional[str]:
    """The bundle id of the application in front, from LaunchServices (``lsappinfo``), or None when it
    cannot be read. It needs no grant, and every call asks afresh (AppKit's frontmostApplication keeps
    its first answer inside a process with no run loop). On a locked screen it is ``LOGIN_WINDOW``."""
    import re   # noqa: PLC0415
    try:
        asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True,
                             timeout=5, env=env).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "bundleid", asn],
                             capture_output=True, text=True, timeout=5, env=env).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"CFBundleIdentifier"="([^"]*)"', out)
    return m.group(1) if m else None


def hid_lock() -> SessionLock:
    """The lock that keeps the virtual keyboard to one typist across every process."""
    return SessionLock(HID_LOCK, label="the virtual keyboard")


def helper_argv(helper: str | os.PathLike, *flags: str, sudo: bool = True) -> list[str]:
    """The command line that runs vhid_type with the given flags."""
    allowed = {"--wake", "--clear", "--return"}
    bad = [f for f in flags if f not in allowed]
    if bad:
        raise ValueError(f"unknown vhid_type flag(s): {bad}")
    return (["sudo", "-n"] if sudo else []) + [str(helper), *flags]


def _type(helper: str, text: str, flags: tuple[str, ...], timeout: float,
          sudo: bool, *, env: Mapping[str, str] | None = None) -> tuple[bool, Optional[str]]:
    """Run the helper once. Returns ``(could_run, error)``. ``error`` is None when it typed.

    ``could_run`` is False when the helper timed out or could not start at all, and True when it ran
    (even if it then reported a failure)."""
    try:
        r = _run(helper_argv(helper, *flags, sudo=sudo), input=text, text=True,
                 capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "the virtual HID helper timed out"
    except OSError as e:
        return False, f"could not run the virtual HID helper: {str(e)[:50]}"
    if r.returncode != 0 and "typed" not in (r.stdout or ""):
        return True, f"virtual HID helper failed: {(r.stderr or r.stdout or '').strip()[:80]}"
    return True, None


def _wake(*, env: Mapping[str, str] | None = None) -> None:
    try:
        _popen(["/usr/bin/caffeinate", "-u", "-t", "30"], env=env)
    except OSError:
        pass


def unlock(password: str, *, helper: str | os.PathLike | None = None, settle: float = 2.0,
           poll_seconds: float = 8.0, attempts: int = 2,
           locked: Callable[[], Optional[bool]] | None = None, sudo: bool = True, env: Mapping[str, str] | None = None,
           front: Callable[[], Optional[str]] | None = None, lock: SessionLock | None = None,
           wait_s: float = HID_WAIT_S) -> Result:
    """Type ``password`` at the lock screen and observe the result. Returns ``(ok, detail)``.

    The display is woken first, then the helper taps Shift (to show the password field), clears the
    field, types the password and presses Return. The first keys at a lock screen are often lost
    because the field has not taken focus yet, so when the session is still locked after
    ``poll_seconds`` the display is woken again and the password typed once more, up to
    ``attempts`` tries in total.

    It holds the virtual keyboard's lock (``lock``, by default ``hid_lock()``) for the whole time, waits
    up to ``wait_s`` seconds for another typist, and once it holds the lock looks again: a Mac another
    typist unlocked meanwhile is "Already unlocked.", with nothing typed. Before each round of typing
    the login window must be in front (``front``, by default ``front_app``), or nothing is typed.

    ``locked`` replaces ``is_locked`` (for a caller with its own observer)."""
    locked = locked or is_locked
    if not password:
        return False, "no password given"
    if locked() is False:
        return True, "Already unlocked."
    helper = str(paths.vhid_path(helper))
    if not _exists(helper):
        return False, f"virtual HID helper not built ({helper})"
    front = front or (lambda: front_app(env=env))
    try:
        with (lock or hid_lock()).hold("unlock the Mac", wait_s):
            return _unlock_held(password, helper, settle, poll_seconds, attempts, locked, front, sudo, env)
    except Busy as e:
        return False, f"another program is typing through the virtual keyboard: {e}"


def _at_lock_screen(locked: Callable[[], Optional[bool]], front: Callable[[], Optional[str]]) -> Optional[str]:
    """None when the keys may go out now (the session reads locked, or cannot be read, and the login
    window is in front), else why not. "unlocked" when the session reads unlocked."""
    if locked() is False:
        return "unlocked"
    app = front()
    if app != LOGIN_WINDOW:
        return (f"the lock screen is not in front ({app or 'the front application cannot be read'}), "
                f"so nothing was typed")
    return None


def _unlock_held(password, helper, settle, poll_seconds, attempts, locked, front, sudo, env) -> Result:
    """unlock, while this process holds the virtual keyboard."""
    # ⛔ LOOKED AT AGAIN ONCE THE LOCK IS HELD: a typist that waited for another finds the Mac unlocked
    if locked() is False:
        return True, "Already unlocked."
    _wake(env=env)
    _sleep(settle)

    helper_error = ""
    for attempt in range(max(1, attempts)):
        # ⛔ AND RIGHT BEFORE THE KEYS GO OUT, after the wake and the wait: Cmd+A and Backspace in any
        # other application select everything in it and delete it
        why = _at_lock_screen(locked, front)
        if why == "unlocked":
            return True, "Already unlocked." if attempt == 0 else "Unlocked."
        if why:
            return False, why
        ran, err = _type(helper, password, ("--wake", "--clear", "--return"), 45, sudo, env=env)
        if not ran:
            return False, err or "the virtual HID helper did not run"
        if err:
            helper_error = err

        deadline = _now() + poll_seconds
        while _now() < deadline:
            _sleep(0.5)
            if locked() is False:
                return True, "Unlocked." if attempt == 0 else "Unlocked (took a second try)."

        if attempt < attempts - 1:
            _wake(env=env)
            _sleep(0.8)

    return False, helper_error or (f"typed the password at the lock screen, but it is still locked "
                                   f"after {attempts} tries (the password may be wrong)")


def relock(*, poll_seconds: float = 5.0,
           locked: Callable[[], Optional[bool]] | None = None, env: Mapping[str, str] | None = None) -> Result:
    """Lock the console and observe it. Returns ``(ok, detail)``. Nothing is typed.

    ``pmset displaysleepnow`` sleeps the display, and the session locks if the Mac is set to require
    a password right after sleep. That setting belongs to the Mac, so the lock is not assumed: the
    lock bit is watched until it sets."""
    locked = locked or is_locked
    if locked() is True:
        return True, "Already locked."
    try:
        r = _run(["/usr/bin/pmset", "displaysleepnow"], capture_output=True, text=True, timeout=10, env=env)
    except subprocess.TimeoutExpired:
        return False, "pmset timed out"
    except OSError as e:
        return False, f"could not run pmset: {str(e)[:50]}"
    if r.returncode != 0:
        return False, f"pmset failed: {(r.stderr or r.stdout).strip()[:80]}"

    deadline = _now() + poll_seconds
    while _now() < deadline:
        _sleep(0.5)
        state = locked()
        if state is True:
            return True, "Locked."
        if state is None:
            return False, "the display slept, but the lock cannot be observed from here"
    return False, "the display slept, but the console did not lock"


NO_PROMPT = "No password prompt is open on the Mac right now."


def use_password(*, helper: str | os.PathLike | None = None,
                 prompt_present: Callable[[], object] | None = None, sudo: bool = True, env: Mapping[str, str] | None = None,
                 lock: SessionLock | None = None, wait_s: float = HID_WAIT_S) -> Result:
    """Switch a Touch ID first credential panel to its password field. Nothing is typed.

    Some panels open on the Touch ID screen with a "Use Password..." button and no field yet. That
    button is the panel's default action, so a Return through the virtual keyboard activates it.
    ``prompt_present`` returns something truthy while a panel is on screen (default:
    ``claude_human.screenshot.auth_prompt``)."""
    prompt_present = prompt_present or _default_prompt
    if not prompt_present():
        return False, NO_PROMPT
    helper = str(paths.vhid_path(helper))
    if not _exists(helper):
        return False, f"virtual HID helper not built ({helper})"
    try:
        with (lock or hid_lock()).hold("switch a password panel to its field", wait_s):
            if not prompt_present():       # another typist may have answered it meanwhile
                return False, NO_PROMPT
            _, err = _type(helper, "", ("--return",), 20, sudo, env=env)
    except Busy as e:
        return False, f"another program is typing through the virtual keyboard: {e}"
    if err:
        return False, err
    return True, "Asked the Mac to switch to the password field."


def approve(password: str, *, helper: str | os.PathLike | None = None, poll_seconds: float = 7.0,
            prompt_present: Callable[[], object] | None = None, sudo: bool = True, env: Mapping[str, str] | None = None,
            lock: SessionLock | None = None, wait_s: float = HID_WAIT_S) -> Result:
    """Type ``password`` into the credential panel on screen and observe it close.

    The OK button of the LocalAuthentication panel ignores Return, and synthetic clicks never reach
    a secure input panel at all. Space presses the focused button, and the tab order is field,
    Cancel, OK, so the helper types the password, two Tabs and a Space in one burst (after clearing
    the field). This needs macOS Full Keyboard Access, so that Tab reaches buttons.

    A wrong password leaves the panel up, which is reported as a failure."""
    prompt_present = prompt_present or _default_prompt
    if not password:
        return False, "no password given"
    if not prompt_present():
        return False, NO_PROMPT
    helper = str(paths.vhid_path(helper))
    if not _exists(helper):
        return False, f"virtual HID helper not built ({helper})"
    try:
        with (lock or hid_lock()).hold("type a password into a password panel", wait_s):
            if not prompt_present():       # another typist may have answered it meanwhile
                return False, NO_PROMPT
            _, err = _type(helper, password + "\t\t ", ("--clear",), 45, sudo, env=env)
            if err:
                return False, err
            deadline = _now() + poll_seconds
            while _now() < deadline:
                _sleep(0.4)
                if not prompt_present():
                    return True, "Approved."
    except Busy as e:
        return False, f"another program is typing through the virtual keyboard: {e}"
    return False, ("typed the password into the prompt, but it is still open (the password may be "
                   "wrong, or the prompt is waiting for Touch ID)")
