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

#: Hooks for the outside world, replaced by tests.
_run = subprocess.run
_popen = subprocess.Popen
_sleep = time.sleep
_now = time.time
_exists = os.path.exists

Result = tuple[bool, str]


def is_locked() -> Optional[bool]:
    """Whether the console session is locked. None when it cannot be read (no Quartz)."""
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked"))
    except Exception:
        return None


def _default_prompt() -> Optional[dict]:
    from .screenshot import auth_prompt
    return auth_prompt()


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
           locked: Callable[[], Optional[bool]] | None = None, sudo: bool = True, env: Mapping[str, str] | None = None) -> Result:
    """Type ``password`` at the lock screen and observe the result. Returns ``(ok, detail)``.

    The display is woken first, then the helper taps Shift (to show the password field), clears the
    field, types the password and presses Return. The first keys at a lock screen are often lost
    because the field has not taken focus yet, so when the session is still locked after
    ``poll_seconds`` the display is woken again and the password typed once more, up to
    ``attempts`` tries in total.

    ``locked`` replaces ``is_locked`` (for a caller with its own observer)."""
    locked = locked or is_locked
    if not password:
        return False, "no password given"
    if locked() is False:
        return True, "Already unlocked."
    helper = str(paths.vhid_path(helper))
    if not _exists(helper):
        return False, f"virtual HID helper not built ({helper})"

    _wake(env=env)
    _sleep(settle)

    helper_error = ""
    for attempt in range(max(1, attempts)):
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
                 prompt_present: Callable[[], object] | None = None, sudo: bool = True, env: Mapping[str, str] | None = None) -> Result:
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
    _, err = _type(helper, "", ("--return",), 20, sudo, env=env)
    if err:
        return False, err
    return True, "Asked the Mac to switch to the password field."


def approve(password: str, *, helper: str | os.PathLike | None = None, poll_seconds: float = 7.0,
            prompt_present: Callable[[], object] | None = None, sudo: bool = True, env: Mapping[str, str] | None = None) -> Result:
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
    _, err = _type(helper, password + "\t\t ", ("--clear",), 45, sudo, env=env)
    if err:
        return False, err

    deadline = _now() + poll_seconds
    while _now() < deadline:
        _sleep(0.4)
        if not prompt_present():
            return True, "Approved."
    return False, ("typed the password into the prompt, but it is still open (the password may be "
                   "wrong, or the prompt is waiting for Touch ID)")
