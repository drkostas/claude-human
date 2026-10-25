"""Whether a Mac comes back to the desktop by itself after a restart.

Two settings decide it. With FileVault on, a restart stops at the pre-boot unlock screen, with no
network and nothing running, until a person types the disk password at the keyboard. Without
automatic login, the restart reaches the login window and waits there, with the user's launch agents
not started and the login keychain closed. A Mac that an agent reaches remotely, and that must come
back from a crash or an update with nobody at it, needs FileVault off and automatic login set for one
user. Both are the person's to change, since each takes their password; this module only reads them,
so the record of the machine says whether a restart needs a person, and a planned restart can wait
until it does not.

    from claude_human import unattended
    unattended.filevault_on()        # True, False, or None when fdesetup did not answer
    unattended.autologin_user()      # "alice", "" when none is set, None when the file cannot be read
    unattended.read()                # both, whether the login password is stored, and a verdict:
                                     # {"filevault_on": False, "autologin_user": "alice",
                                     #  "password_stored": True, "unattended": True,
                                     #  "words": "FileVault off; automatic login for alice"}

`claude-human unattended` prints that dict. The verdict is None, never False, while any part is
unknown: a reader that could not run says nothing about the machine.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess

FDESETUP = "/usr/bin/fdesetup"
#: the login window's settings; world-readable, so no sudo is needed
LOGINWINDOW = "/Library/Preferences/com.apple.loginwindow.plist"
#: where macOS stores the automatic login password (obfuscated, root only). Automatic login needs
#: both this file and the user's name in the login window's settings; System Settings writes both
KCPASSWORD = "/etc/kcpassword"


def parse_fdesetup(text: str) -> bool | None:
    """`fdesetup status` as a flag: "FileVault is On." True, "FileVault is Off." False, anything else
    None. A second line ("Encryption in progress: Percent completed = 40") does not change it: the
    first line is what the next boot does."""
    m = re.search(r"FileVault is (On|Off)\b", text or "")
    return None if m is None else m.group(1) == "On"


def filevault_on(*, run=subprocess.run, env: dict | None = None) -> bool | None:
    """Whether FileVault is on, from `fdesetup status` (no root needed). None when it did not answer
    (not a Mac, the command missing or failing). `env` is the environment the command runs with."""
    try:
        r = run([FDESETUP, "status"], capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    return parse_fdesetup(getattr(r, "stdout", "") or "")


def parse_loginwindow(data: bytes) -> str:
    """The automatic login user named in the login window's settings, "" when none is set."""
    plist = plistlib.loads(data) or {}
    user = plist.get("autoLoginUser") if isinstance(plist, dict) else None
    return str(user) if isinstance(user, (str, bytes)) and user else ""


def autologin_user(path: str = LOGINWINDOW) -> str | None:
    """The user macOS logs in by itself at boot, "" when automatic login is off, None when the settings
    file cannot be read (not a Mac, or not a plist)."""
    try:
        with open(path, "rb") as f:
            return parse_loginwindow(f.read())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def password_stored(path: str = KCPASSWORD) -> bool:
    """Whether the automatic login password file exists. It is root only, so this reads its presence,
    never its content; a user named for automatic login without it still gets the login window."""
    return os.path.exists(path)


def boots_unattended(filevault: bool | None, user: str | None, stored: bool | None = True) -> bool | None:
    """The verdict: FileVault off, a user named for automatic login and the password stored. None while
    any of the three is unknown, so an unreadable machine never reads as either answer."""
    if filevault is None or user is None or stored is None:
        return None
    return filevault is False and bool(user) and bool(stored)


def words(filevault: bool | None, user: str | None, stored: bool | None = True) -> str:
    """One line for a person: "FileVault on; no automatic login"."""
    fv = "FileVault unknown" if filevault is None else ("FileVault on" if filevault else "FileVault off")
    if user is None:
        al = "automatic login unknown"
    elif not user:
        al = "no automatic login"
    elif stored is False:
        al = f"automatic login for {user} with no stored password"
    else:
        al = f"automatic login for {user}"
    return f"{fv}; {al}"


def read(*, run=subprocess.run, env: dict | None = None, loginwindow: str = LOGINWINDOW,
         kcpassword: str = KCPASSWORD) -> dict:
    """Everything in one dict: filevault_on, autologin_user, password_stored, unattended, words."""
    fv = filevault_on(run=run, env=env)
    user = autologin_user(loginwindow)
    stored = password_stored(kcpassword)
    return {"filevault_on": fv, "autologin_user": user, "password_stored": stored,
            "unattended": boots_unattended(fv, user, stored), "words": words(fv, user, stored)}
