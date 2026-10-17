"""Mission Control desktops (spaces) and the windows on them, asked of yabai.

CGWindowList sees only the desktop that is showing (measured: it reported two windows while yabai
reported sixteen), so it answers "can this be rendered right now", never "does this window exist and
where is it". This module asks the second question, through `yabai -m query`, and every command
verifies its effect by asking again, because yabai has exited 0 having done nothing more than once
(the bottled 7.1.x's `space --create` on newer macOS needs a HEAD build; `space --focus` is ignored
during a space animation).

- Labels, never indices, leave this module as an identifier: indices renumber when yabai restarts or
  the user reorders desktops. A space's `uuid` is macOS's own identity and survives both; labels live
  in yabai's memory, so `relabel(uuid, label)` puts a forgotten one back.
- Nothing works while the console is locked: `query --windows` still lists (stale), but every command
  answers "could not locate the window to act on!". A locked screen also truncates a query's output
  ("[" and nothing after), which raises here as unreadable rather than reading as an empty machine.
- `focus` is the one function that moves the user's screen. Give it exactly one caller.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping

YABAI = "/opt/homebrew/bin/yabai"


def _run(args: list[str], *, yabai: str = YABAI, env: Mapping[str, str] | None = None):
    return subprocess.run([yabai, "-m", *args], capture_output=True, text=True, timeout=10, env=env)


def query(what: str, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> list[dict]:
    """Ask yabai, and fail loudly: an empty or truncated answer is an unreadable machine (often a
    locked screen), never an empty one."""
    r = _run(["query", what], yabai=yabai, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"yabai query {what}: {r.stderr.strip()[:200] or 'no output'}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"yabai query {what}: unreadable answer ({r.stdout.strip()[:20]!r}); "
                           f"the screen may be locked") from None


def _space(*args: str, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> None:
    r = _run(["space", *args], yabai=yabai, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"yabai space {' '.join(args)}: {r.stderr.strip()[:200] or 'failed'}")


def spaces(*, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> list[dict]:
    """Every desktop: {"uuid", "index", "label", "visible"}. The uuid survives a renumbering and a
    yabai restart; the label does not, so record the uuid for anything that must find the desktop
    again later."""
    return [{"uuid": s["uuid"], "index": s["index"], "label": s.get("label") or "",
             "visible": bool(s.get("is-visible"))} for s in query("--spaces", yabai=yabai, env=env)]


def windows(*, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> list[dict]:
    """Every window on every desktop — the thing CGWindowList cannot answer."""
    return [{"id": w["id"], "space": w.get("space"), "app": w.get("app") or "",
             "title": w.get("title") or ""} for w in query("--windows", yabai=yabai, env=env)]


def index_of(label: str, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> int | None:
    """A label's index right now; None when no desktop carries it, never a guess (a plausible number
    would address whatever desktop sits at that position today)."""
    if not label:
        return None
    return next((s["index"] for s in spaces(yabai=yabai, env=env) if s["label"] == label), None)


def create(label: str, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> dict:
    """Make a new desktop carrying `label`; returns {"index", "uuid"}. The new desktop is found by
    the delta of uuids, never by "the last index": with two displays the new space is inserted
    mid-sequence and every later index shifts. Refuses a label that already exists, which would make
    every placement built on `index_of` ambiguous."""
    if not label:
        raise ValueError("a desktop needs a label")
    before = spaces(yabai=yabai, env=env)
    if any(s["label"] == label for s in before):
        raise RuntimeError(f"a desktop labelled {label!r} already exists")
    _space("--create", yabai=yabai, env=env)
    after = spaces(yabai=yabai, env=env)
    new = [s for s in after if s["uuid"] not in {b["uuid"] for b in before}]
    if len(after) != len(before) + 1 or len(new) != 1:
        raise RuntimeError(f"space --create exited 0 but the desktop count went "
                           f"{len(before)} -> {len(after)}; nothing was created")
    _space(str(new[0]["index"]), "--label", label, yabai=yabai, env=env)
    idx = index_of(label, yabai=yabai, env=env)
    if idx is None:
        raise RuntimeError(f"created desktop {new[0]['index']} but the label {label!r} did not stick")
    return {"index": idx, "uuid": new[0]["uuid"]}


def relabel(uuid: str, label: str, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> bool:
    """Put `label` back on the desktop macOS knows as `uuid`. False when no such desktop exists any
    more — the one case where "the desktop is gone" is true rather than assumed."""
    idx = next((s["index"] for s in spaces(yabai=yabai, env=env) if s["uuid"] == uuid), None)
    if idx is None:
        return False
    _space(str(idx), "--label", label, yabai=yabai, env=env)
    return index_of(label, yabai=yabai, env=env) == idx


def label_for(uuid: str, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> str | None:
    """The label a command can address the desktop `uuid` by: its own, or a stable one made from the
    uuid ("d-<first part>") put on it now when it has none. None when no such desktop exists. yabai
    takes a number or a label, never a uuid, and the number changes under the user's hands."""
    s = next((x for x in spaces(yabai=yabai, env=env) if x["uuid"] == uuid), None)
    if s is None:
        return None
    if s["label"]:
        return s["label"]
    label = "d-" + uuid.split("-")[0].lower()
    return label if relabel(uuid, label, yabai=yabai, env=env) else None


def destroy(label: str, uuid: str | None = None, *, yabai: str = YABAI,
            env: Mapping[str, str] | None = None) -> bool:
    """Remove the desktop carrying `label`. True if it is gone afterwards, whether or not it was
    there to begin with, so a cleanup run twice does not fail the second time. A forgotten label is
    not a gone desktop: with the `uuid` recorded at creation, the label is put back first. macOS
    refuses to destroy the visible space; that raises, because the user may be looking at it."""
    if index_of(label, yabai=yabai, env=env) is None:
        if uuid is None or not relabel(uuid, label, yabai=yabai, env=env):
            return True
    _space(label, "--destroy", yabai=yabai, env=env)
    return index_of(label, yabai=yabai, env=env) is None


def move_to(label: str, index: int, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> bool:
    """Put the desktop carrying `label` at position `index` (1 first); True once a re-read says so."""
    if index_of(label, yabai=yabai, env=env) == index:
        return True
    _space(label, "--move", str(index), yabai=yabai, env=env)
    return index_of(label, yabai=yabai, env=env) == index


def windows_on(label: str, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> list[dict]:
    """The windows on one labelled desktop; [] for a label no desktop carries, indistinguishable
    from an empty desktop on purpose: both mean nothing of yours is there."""
    idx = index_of(label, yabai=yabai, env=env)
    return [] if idx is None else [w for w in windows(yabai=yabai, env=env) if w["space"] == idx]


def visible_label(*, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> str:
    """The label of the desktop the user is looking at."""
    return next((s["label"] for s in spaces(yabai=yabai, env=env) if s["visible"]), "")


def focus(index: int, *, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> bool:
    """Show the desktop at `index`; True once it is actually the visible one. The only function here
    that moves the user's screen: give it one caller, and have that caller ask first. Verified,
    because macOS ignores a focus request during a space animation and yabai still exits 0."""
    _space("--focus", str(index), yabai=yabai, env=env)
    return any(s["visible"] and s["index"] == index for s in spaces(yabai=yabai, env=env))
