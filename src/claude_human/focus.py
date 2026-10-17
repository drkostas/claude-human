"""Do Not Disturb while an agent drives a window, and the person's own Focus kept.

A notification banner that slides in while a window is driven is shown to nobody and swallows the
next click meant for that window, so an agent turns Do Not Disturb on for the duration and puts the
Focus state back after. macOS has no CLI for this: there is no `focus` or `dnd` command, and the
`defaults` domain often recommended is not read by the Focus daemon. The one supported path for a
script is the Shortcuts app's `Set Focus` action run through the `shortcuts` CLI, so two shortcuts
must exist first (one Set Focus on, one off; creating them is a GUI act), named by `on_name` and
`off_name`. Until they exist, `begin` answers {"ok": False, ...} and says what to make, never a
silent pass whose clicks a banner can still steal.

The outcome is observed, never asserted: the Focus daemon's own assertion store is read back after
each shortcut run. And a Focus the person had running is never cancelled: it already suppresses
banners, and the two shortcuts could not put it back.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

#: the Focus daemon's own assertion store. READ ONLY: it is the daemon's record of what is on,
#: and writing it changes nothing the daemon will notice.
ASSERTIONS = Path.home() / "Library" / "DoNotDisturb" / "DB" / "Assertions.json"
SHORTCUTS = "/usr/bin/shortcuts"
ON_NAME, OFF_NAME = "focus on", "focus off"


def active(assertions: Path = ASSERTIONS) -> list[str]:
    """The Focus mode identifiers macOS has asserted right now, such as
    ["com.apple.donotdisturb.mode.default"]; [] when none. An empty list and a missing file read
    the same on purpose: the store has no records key when nothing is asserted, and a Mac that has
    never used Focus has no file."""
    try:
        data = json.loads(assertions.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: list[str] = []
    for record in data.get("data") or []:
        for assertion in record.get("storeAssertionRecords") or []:
            mode = (assertion.get("assertionDetails") or {}).get("assertionDetailsModeIdentifier")
            if mode and mode not in out:
                out.append(mode)
    return out


def _names(*, env: Mapping[str, str] | None = None) -> list[str]:
    try:
        r = subprocess.run([SHORTCUTS, "list"], capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def installed(on_name: str = ON_NAME, off_name: str = OFF_NAME, *,
              env: Mapping[str, str] | None = None) -> list[str]:
    """Which of the two shortcuts exist. Both are needed: Do Not Disturb that cannot be turned off
    is worse than none."""
    have = set(_names(env=env))
    return [n for n in (on_name, off_name) if n in have]


def set_dnd(on: bool, on_name: str = ON_NAME, off_name: str = OFF_NAME, *,
            env: Mapping[str, str] | None = None, assertions: Path = ASSERTIONS) -> tuple[bool, str]:
    """Run the shortcut. True only when it ran AND the assertion store agrees afterwards."""
    name = on_name if on else off_name
    try:
        r = subprocess.run([SHORTCUTS, "run", name], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not run {name!r}: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or f"{name!r} failed").strip()
    now = bool(active(assertions))
    if now is not on:
        return False, f"{name!r} ran, but Focus is {'on' if now else 'off'}"
    return True, f"Do Not Disturb {'on' if on else 'off'}"


def begin(on_name: str = ON_NAME, off_name: str = OFF_NAME, *,
          env: Mapping[str, str] | None = None, assertions: Path = ASSERTIONS) -> dict:
    """Turn Do Not Disturb on, and say what was running before: {"before", "ok", "changed",
    "detail"}. Keep `before` somewhere durable and give it back to `restore`, so a crashed session
    can still be undone by the next one. A Focus already running is left alone and counts as ok."""
    before = active(assertions)
    if before:
        return {"before": before, "ok": True, "changed": False,
                "detail": f"a Focus was already on ({', '.join(before)}); left alone"}
    if len(installed(on_name, off_name, env=env)) < 2:
        return {"before": before, "ok": False, "changed": False,
                "detail": f"no Focus shortcuts: make {on_name!r} and {off_name!r} in Shortcuts "
                          f"(one Set Focus action each)"}
    ok, detail = set_dnd(True, on_name, off_name, env=env, assertions=assertions)
    return {"before": before, "ok": ok, "changed": ok, "detail": detail}


def restore(before: list[str] | None, on_name: str = ON_NAME, off_name: str = OFF_NAME, *,
            env: Mapping[str, str] | None = None, assertions: Path = ASSERTIONS) -> dict:
    """Put Focus back the way `before` says it was. Only ever turns Do Not Disturb off, and only
    when `begin` turned it on: `before` non-empty is the person's own Focus (not cancelled), and
    None means no `begin` ever ran, so there is nothing to undo."""
    if before is None:
        return {"ok": True, "changed": False, "detail": "nothing had begun"}
    if before:
        return {"ok": True, "changed": False,
                "detail": f"left the Focus that was already running ({', '.join(before)})"}
    if not active(assertions):
        return {"ok": True, "changed": False, "detail": "Do Not Disturb is already off"}
    if len(installed(on_name, off_name, env=env)) < 2:
        return {"ok": False, "changed": False,
                "detail": f"Do Not Disturb is on and {off_name!r} does not exist"}
    ok, detail = set_dnd(False, on_name, off_name, env=env, assertions=assertions)
    return {"ok": ok, "changed": ok, "detail": detail}
