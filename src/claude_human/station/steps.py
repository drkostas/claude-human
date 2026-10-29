"""A step session: drive one window through a running station, a step at a time, with a picture after
each, for a program that works or reads in an app it cannot see or touch itself.

The program writes each step as a file, `OUTDIR/cmd-<n>.json` from 1 on, and reads the result from
`OUTDIR/done-<n>.json` (atomically written), so it can be a different process, or an agent that
decides each step after looking at the last picture. The session:

  - waits until the Mac's session is unlocked (`wait_unlock` seconds at most);
  - keeps the display awake for its whole time with `caffeinate -u -d` (unless `awake=False`),
    since a sleeping display can lock the session in the middle of the work;
  - finds the window (by its id, or the largest window of an app the station lists), opening the
    app first with `open -a` when asked, and writes `ready.json` with a picture;
  - runs the steps in order, bringing the window to the front before each input step (a window on
    another desktop answers no click), and saves a picture, and its text lines when the Vision
    framework can read them, after each;
  - ends at a `stop` step, when no step arrives within `step_wait` seconds, after `total` seconds,
    or on a signal, and on every way out stops keeping the display awake and runs `on_exit`
    (`relock=True` locks the session again).

Steps (fractions of the window for every point):

    {"click": [x, y]}                     a click; "double": [x, y] for a double click
    {"key": code, "flags": f}             one key press (macOS virtual key code, modifier flags)
    {"text": "words"}                     text pasted into the window
    {"scroll": {"dy": -3, "unit": "line", "x": 0.5, "y": 0.5}}       one scroll ("phased": true too)
    {"page": {"dy": 600, "max": 20, ...}}  scroll until the picture stops changing, a picture a page
    {"raise": true}                       bring the window to the front
    {"wait": seconds}                     wait, then a picture
    {"noop": true}                        nothing, only resets the step timer (a keepalive)
    {"stop": true}

Every input goes through the station, which refuses while the session is locked, a system dialog is
up, or another window covers the point, and each answer is in the done file. Nothing here decides
what to do: it does what each step says and reports what it saw.

    claude-human steps OUTDIR (--window ID | --app NAME) [--open] [--total S] [--step-wait S] [--relock]
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, Optional

from . import client

INPUTS = ("click", "double", "key", "text", "scroll", "page")


def _write(folder: str, name: str, obj: Any) -> None:
    tmp = os.path.join(folder, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(folder, name))


def _locked() -> Optional[bool]:
    from .. import screenshot   # noqa: PLC0415
    return screenshot.screen_locked()


def _lines(path: str) -> Optional[list]:
    try:
        from .. import ocr   # noqa: PLC0415
        found = ocr.lines(path, accurate=True)
    except Exception:  # noqa: BLE001 - no Vision framework here: the picture is still saved
        return None
    if found is None:
        return None
    return [{"text": d["text"], "mid": [round(v, 3) for v in ocr.middle(d)]} for d in found]


def _keep_awake(seconds: float):
    return subprocess.Popen(["/usr/bin/caffeinate", "-u", "-d", "-t", str(int(seconds) + 60)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


class StepSession:
    """One session in `out`, for `window` (an id) or the largest window of `app`. `send`, `shot`,
    `windows`, `locked`, `keep_awake`, `lines`, `opener`, `sleep` and `clock` stand in for the station
    client, the lock bit, caffeinate, the text reader, `open -a` and time, in a test."""

    def __init__(self, out: str, *, token: str, window: Optional[int] = None, app: Optional[str] = None,
                 open_app: bool = False, total: float = 1500.0, step_wait: float = 480.0,
                 wait_unlock: float = 3600.0, awake: bool = True, relock: bool = False,
                 station: str = client.STATION, send: Callable | None = None, shot: Callable | None = None,
                 windows: Callable | None = None, locked: Callable | None = None,
                 keep_awake: Callable | None = None, lines: Callable | None = None,
                 opener: Callable | None = None, on_exit: Callable | None = None,
                 sleep: Callable = time.sleep, clock: Callable = time.monotonic, log: Callable = print):
        if not window and not app:
            raise ValueError("name the window or the app")
        self.out, self.token, self.station = out, token, station
        self.window, self.app, self.open_app = window, app, open_app
        self.total, self.step_wait, self.wait_unlock = total, step_wait, wait_unlock
        self.awake, self.relock = awake, relock
        self._send = send or (lambda msg: client.send(msg, token=token, station=station))
        self._shot = shot or (lambda wid: client.shot(wid, token=token, station=station, full=True))
        self._windows = windows or (lambda: client.windows(token=token, station=station))
        self._locked = locked or _locked
        self._keep_awake = keep_awake or _keep_awake
        self._lines = lines or _lines
        self._open = opener or (lambda name: subprocess.run(["/usr/bin/open", "-a", name], timeout=30))
        self._on_exit = on_exit
        self.sleep, self.clock, self.log = sleep, clock, log
        self._awake_proc = None
        self._ended = False

    # ------------------------------------------------------------------ helpers

    def _find(self) -> Optional[int]:
        if self.window:
            return int(self.window)
        listed = self._windows() or []
        want = (self.app or "").lower()
        mine = [w for w in listed if str(w.get("app", "")).lower() == want] or \
               [w for w in listed if want in str(w.get("app", "")).lower()]
        if not mine:
            return None
        return int(max(mine, key=lambda w: int(w.get("width", 0)) * int(w.get("height", 0)))["id"])

    def picture(self, name: str) -> tuple[Optional[list], str]:
        """A picture of the window saved as OUTDIR/<name>.jpg: (its text lines or None, a digest),
        or (None, "") when the station gave none."""
        got = self._shot(self.window)
        if not got:
            return None, ""
        path = os.path.join(self.out, name + ".jpg")
        with open(path, "wb") as f:
            f.write(got[0])
        return self._lines(path), hashlib.sha256(got[0]).hexdigest()

    def _input(self, msg: dict) -> dict:
        return self._send({**msg, "window": int(self.window)})

    def end(self, why: str) -> None:
        """Stop keeping the display awake and run the exit hooks, once."""
        if self._ended:
            return
        self._ended = True
        if self._awake_proc is not None:
            try:
                self._awake_proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        if self.relock:
            from ..unlock import relock   # noqa: PLC0415
            ok, said = relock()
            self.log(f"relock: {'ok' if ok else 'failed'} {said}")
        if self._on_exit is not None:
            self._on_exit()
        self.log(f"ended: {why}")

    # ------------------------------------------------------------------ one step

    def step(self, n: int, cmd: dict) -> dict:
        """Run one step and say what happened: {"cmd", "answers", "pages"}."""
        done: dict = {"cmd": cmd, "answers": [], "pages": []}

        def snap(name: str) -> str:
            found, digest = self.picture(name)
            done["pages"].append({"name": name, "lines": found} if digest else {"name": name, "missing": True})
            return digest

        if any(k in cmd for k in INPUTS):
            done["answers"].append(self._input({"type": "raise"}))
        if "click" in cmd or "double" in cmd:
            x, y = cmd.get("click") or cmd.get("double")
            done["answers"].append(self._input({"type": "dblclick" if "double" in cmd else "click",
                                                "x": float(x), "y": float(y)}))
            self.sleep(float(cmd.get("settle", 1.5)))
            snap(f"step-{n}")
        elif "key" in cmd:
            done["answers"].append(self._input({"type": "key", "code": int(cmd["key"]),
                                                "flags": int(cmd.get("flags", 0))}))
            self.sleep(float(cmd.get("settle", 1.0)))
            snap(f"step-{n}")
        elif "text" in cmd:
            done["answers"].append(self._input({"type": "text", "text": str(cmd["text"])}))
            self.sleep(float(cmd.get("settle", 1.5)))
            snap(f"step-{n}")
        elif "scroll" in cmd:
            done["answers"].append(self._input({"type": "scroll", **_scroll(cmd["scroll"])}))
            self.sleep(float(cmd.get("settle", 1.0)))
            snap(f"step-{n}")
        elif "page" in cmd:
            spec = cmd["page"] if isinstance(cmd["page"], dict) else {}
            last = snap(f"step-{n}-p000")
            for i in range(1, int(spec.get("max", 20)) + 1):
                done["answers"].append(self._input({"type": "scroll", **_scroll(spec)}))
                self.sleep(float(spec.get("settle", 1.2)))
                name = f"step-{n}-p{i:03d}"
                digest = snap(name)
                if digest and digest == last:
                    done["pages"].pop()
                    try:
                        os.remove(os.path.join(self.out, name + ".jpg"))
                    except OSError:
                        pass
                    done["end"] = "the picture stopped changing"
                    break
                last = digest
        elif "raise" in cmd:
            done["answers"].append(self._input({"type": "raise"}))
            snap(f"step-{n}")
        elif "wait" in cmd:
            self.sleep(float(cmd["wait"]))
            snap(f"step-{n}")
        return done

    # ------------------------------------------------------------------ the session

    def run(self) -> int:
        """The whole session: 0 ended by a stop step or its time, 2 the session stayed locked, 3 no
        window, 4 the session locked again during the work."""
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "pid"), "w") as f:
            f.write(str(os.getpid()))
        t0 = self.clock()
        while self._locked() is not False:
            if self.clock() - t0 > self.wait_unlock:
                _write(self.out, "ready.json", {"ok": False, "detail": "the session stayed locked"})
                return 2
            self.sleep(0.3)
        start = self.clock()
        try:
            if self.awake:
                self._awake_proc = self._keep_awake(self.total)
            if self.open_app and self.app:
                self._open(self.app)
            for _ in range(40):
                self.window = self._find()
                if self.window:
                    break
                self.sleep(0.5)
            if not self.window:
                _write(self.out, "ready.json", {"ok": False, "detail": f"no window of {self.app}"})
                return 3
            raised = self._input({"type": "raise"})
            found, _ = self.picture("ready")
            _write(self.out, "ready.json", {"ok": True, "window": self.window, "raise": raised, "lines": found})
            self.log(f"ready: window {self.window}")
            n = 1
            while self.clock() - start < self.total:
                path = os.path.join(self.out, f"cmd-{n}.json")
                asked = self.clock()
                while not os.path.exists(path):
                    if self.clock() - asked > self.step_wait or self.clock() - start > self.total:
                        self.end("no step arrived in time" if self.clock() - start <= self.total else "its time ended")
                        return 0
                    self.sleep(0.5)
                try:
                    with open(path, encoding="utf-8") as f:
                        cmd = json.load(f)
                except (OSError, ValueError):
                    self.sleep(0.2)             # still being written
                    continue
                if not isinstance(cmd, dict) or cmd.get("stop"):
                    self.end("a stop step")
                    return 0
                if self._locked() is True:
                    _write(self.out, f"done-{n}.json", {"cmd": cmd, "locked": True})
                    self.end("the session locked again")
                    return 4
                _write(self.out, f"done-{n}.json", self.step(n, cmd) if not cmd.get("noop") else {"cmd": cmd})
                n += 1
            self.end("its time ended")
            return 0
        finally:
            self.end("it stopped")


def _scroll(spec: dict) -> dict:
    """Pure. A scroll input's fields from a step's spec: dy, dx, the point, and the line or phased modes."""
    out = {"dy": int(spec.get("dy", 0)), "dx": int(spec.get("dx", 0)),
           "x": float(spec.get("x", 0.5)), "y": float(spec.get("y", 0.5))}
    if spec.get("unit") == "line":
        out["unit"] = "line"
    if spec.get("phased"):
        out["phased"] = True
    return out


def main(argv: list[str], *, environ=None) -> int:
    import argparse   # noqa: PLC0415
    from ..keys import _token   # noqa: PLC0415
    p = argparse.ArgumentParser(prog="claude-human steps", description="drive one window through the "
                                "station a step at a time, from step files, with a picture after each")
    p.add_argument("out", help="the folder the step files go into and the pictures and answers come out of")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--window", type=int, help="the window's id")
    who.add_argument("--app", help="the app, whose largest window is used")
    p.add_argument("--open", action="store_true", help="open the app first (open -a)")
    p.add_argument("--total", type=float, default=1500.0, help="seconds the session lasts at most")
    p.add_argument("--step-wait", type=float, default=480.0, help="seconds to wait for the next step file")
    p.add_argument("--wait-unlock", type=float, default=3600.0, help="seconds to wait for an unlocked session")
    p.add_argument("--no-awake", action="store_true", help="do not keep the display awake")
    p.add_argument("--relock", action="store_true", help="lock the session again at the end")
    p.add_argument("--station", default=client.STATION)
    p.add_argument("--token-file", help="the station's token (default: $CLAUDE_HUMAN_STATION_TOKEN, "
                                        "then the station's own token file)")
    a = p.parse_args(argv)
    token = _token(a.token_file, os.environ if environ is None else environ)
    if not token:
        print("refused: no station token (pass --token-file, or set CLAUDE_HUMAN_STATION_TOKEN)")
        return 2
    session = StepSession(a.out, token=token, window=a.window, app=a.app, open_app=a.open, total=a.total,
                          step_wait=a.step_wait, wait_unlock=a.wait_unlock, awake=not a.no_awake,
                          relock=a.relock, station=a.station,
                          log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return session.run()
