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


# ----------------------------------------------------------------------------- one window

def all_windows(*, yabai: str = YABAI, env: Mapping[str, str] | None = None) -> list[dict] | None:
    """yabai's full record of every window (frame, pid, app, title, space, has-ax-reference...), or
    None when yabai did not answer. `windows()` keeps only the id, space, app and title; this keeps
    the frame, which every size check needs."""
    try:
        return query("--windows", yabai=yabai, env=env)
    except Exception:  # noqa: BLE001 - not answering is unknown, never "no windows"
        return None


def space_of_window(window_id: int | None, *, yabai: str = YABAI,
                    env: Mapping[str, str] | None = None) -> str | None:
    """The uuid of the desktop a window is on, or None when it is unknown."""
    if window_id is None:
        return None
    try:
        w = next((w for w in windows(yabai=yabai, env=env) if w["id"] == window_id), None)
        if w is None:
            return None
        return next((s["uuid"] for s in spaces(yabai=yabai, env=env) if s["index"] == w["space"]), None)
    except Exception:  # noqa: BLE001 - not answering is unknown
        return None


def _said(r) -> str:
    return ((r.stderr or r.stdout or "").strip() if r is not None else "yabai did not answer")[:200]


def _window_cmd(window_id: int, *args: str, yabai: str = YABAI, env: Mapping[str, str] | None = None):
    try:
        return _run(["window", str(int(window_id)), *args], yabai=yabai, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def move_window(window_id: int, uuid: str, *, yabai: str = YABAI,
                env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Move one window onto the desktop macOS knows as `uuid`: ("success" | "failure" | "unknown",
    why). Success only when a re-read says so, and a failure carries yabai's own words: on a locked
    screen yabai answers "could not locate the window to act on!" for every window, and a failure
    recorded without that reason cannot be told from a real one."""
    try:
        label = label_for(uuid, yabai=yabai, env=env)    # by its label, never its number
    except Exception as e:  # noqa: BLE001
        return "unknown", f"yabai could not list the desktops: {str(e)[:160]}"
    if label is None:
        return "failure", f"no desktop {uuid} exists any more"
    said = _said(_window_cmd(window_id, "--space", label, yabai=yabai, env=env))
    where = space_of_window(int(window_id), yabai=yabai, env=env)
    if where is None:
        return "unknown", said or "the window could not be found after the move"
    if where == uuid:
        return "success", ""
    return "failure", said or f"the window is still on {where}"


def close_window(window_id: int, *, wait: float = 6.0, window_ids=None, yabai: str = YABAI,
                 env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Close one window with its own close button (through yabai), never its app: (outcome, why).
    Success only when the window server no longer lists the id (`window_ids`, by default
    claude_human.screenshot.window_ids); an app that asks first (unsaved work) keeps its window, and
    that is a failure in its own words, never a forced quit."""
    import time   # noqa: PLC0415
    if window_ids is None:
        from claude_human.screenshot import window_ids   # noqa: PLC0415
    said = _said(_window_cmd(window_id, "--close", yabai=yabai, env=env))
    deadline = time.time() + wait
    while time.time() < deadline:
        ids = window_ids()
        if ids is not None and int(window_id) not in ids:
            return "success", ""
        time.sleep(0.5)
    return "failure", said or "the window is still open"


# ----------------------------------------------------------------------------- window size

def screens() -> list[tuple[tuple, tuple]] | None:
    """Each screen's (frame, usable area) as (x, y, w, h) in Cocoa's coordinates, origin bottom
    left. The usable area leaves out the menu bar and the Dock. None when AppKit cannot say."""
    try:
        import AppKit   # noqa: PLC0415 - only the size checks need it
        out = []
        for s in AppKit.NSScreen.screens():
            f, v = s.frame(), s.visibleFrame()
            out.append(((f.origin.x, f.origin.y, f.size.width, f.size.height),
                        (v.origin.x, v.origin.y, v.size.width, v.size.height)))
        return out or None
    except Exception:  # noqa: BLE001 - no answer is unknown
        return None


def fill_frame(window: dict, found: list[tuple[tuple, tuple]] | None = None) -> dict | None:
    """Pure with `found`. The frame that fills the usable area of the screen this window is on, in
    yabai's coordinates (origin top left), or None when that is unknown. Terminals open new windows
    at a small default size in a corner; this is the size a window "filling the screen beside the
    Dock" has."""
    found = screens() if found is None else found
    fr = (window or {}).get("frame") or {}
    if not found or not fr:
        return None
    main_h = found[0][0][3]          # Cocoa measures y up from the bottom of the first screen
    cx, cy = fr.get("x", 0) + fr.get("w", 0) / 2, fr.get("y", 0) + fr.get("h", 0) / 2
    for (fx, fy, fw, fh), (vx, vy, vw, vh) in found:
        top = main_h - (fy + fh)
        if fx <= cx < fx + fw and top <= cy < top + fh:
            return {"x": vx, "y": main_h - (vy + vh), "w": vw, "h": vh}
    return None


def fills(window: dict, target: dict | None, slack: float = 2.0) -> bool | None:
    """Pure. Whether the window already has this frame; None when the target is unknown."""
    fr = (window or {}).get("frame") or {}
    if target is None or not fr:
        return None
    return all(abs(fr.get(k, -1e9) - target[k]) <= slack for k in ("x", "y", "w", "h"))


def frame_window(window_id: int, target: dict, *, fallback=None, yabai: str = YABAI,
                 env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Give one window this frame: (outcome, why). Success only when a re-read says it has it.

    yabai can lose a window: it stays in its list with an empty title while every command answers
    "could not locate the window". For that case, pass `fallback(window_id, target)`, which sets the
    frame through the app's own scripting and returns the frame it reads back (or None when the app
    does not know the window); a terminal's window bounds are the usual one."""
    said = []
    for flag, arg in (("--move", f"abs:{int(target['x'])}:{int(target['y'])}"),
                      ("--resize", f"abs:{int(target['w'])}:{int(target['h'])}")):
        said.append(_said(_window_cmd(window_id, flag, arg, yabai=yabai, env=env))[:160])
    ws = all_windows(yabai=yabai, env=env)
    w = next((w for w in (ws or []) if w.get("id") == int(window_id)), None)
    if w is not None and fills(w, target):
        return "success", ""
    if fallback is not None:
        got = fallback(int(window_id), target)
        if got is not None:
            return (("success", "") if fills({"frame": got}, target) else
                    ("failure", "; ".join(s for s in said if s) or f"the window is {got}"))
    if ws is None:
        return "unknown", "yabai could not list the windows"
    if w is None:
        return "unknown", "; ".join(s for s in said if s) or "the window could not be found"
    return "failure", "; ".join(s for s in said if s) or f"the window is {w.get('frame')}"
