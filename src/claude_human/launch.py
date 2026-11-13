"""Ask a macOS app bundle for an answer through LaunchServices, with a deadline.

macOS judges a privacy grant (Location, Screen Recording, Accessibility) against the program that asks
for it. A helper started as a plain command is judged as the program that started it, so a helper app
that holds a grant of its own has to be started as an app, through LaunchServices (`open -n -W -a`).
Its answer then cannot come back on standard output, so the helper writes it to a file it is given.

Such a launch can fail without a word. `open -W` may return at once ("Unable to block on
application"), and macOS can leave the new process suspended (state T, a few kilobytes resident) and
never resume it. A caller that only waits for the file then leaves one stopped copy behind on every
call, and a check that runs every few minutes collects hundreds of them.

`ask_app` waits for the answer up to `timeout` seconds. When none came, it finds the copy it started
by the answer file's path on that copy's command line (the path is new for each call, so no other
process names it), ends that pid, and says whether the copy was suspended or running.

    from claude_human import launch
    r = launch.ask_app("/Applications/Helper.app", ["status", "--out", launch.OUT], timeout=30)
    r["answer"]    # the file's text, "" when there was none
    r["outcome"]   # "answered", "suspended", "silent" or "no answer"
    r["pid"]       # the copy that was ended, or None
    r["ended"]     # whether it was ended
    r["said"]      # what open printed, or why it could not run

"suspended": the copy was found stopped and ended. "silent": it was running but wrote nothing in time,
and was ended. "no answer": no file and no copy (it exited without writing, or never started).
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time

#: the token in `args` that `ask_app` replaces with the answer file's path
OUT = "{out}"
OPEN = "/usr/bin/open"
PS = "/bin/ps"


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def copies_naming(path: str, *, run=subprocess.run) -> list[tuple[int, str]]:
    """(pid, state) of each process whose command line has `path` as one whole argument. The state is
    ps's STAT column ("T" for a stopped process). Empty when ps cannot run."""
    try:
        r = run([PS, "-Ao", "pid=,stat=,command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and path in parts[2].split():
            found.append((int(parts[0]), parts[1]))
    return found


def ask_app(app: str, args: list[str], *, timeout: float = 30.0, poll: float = 0.5, run=subprocess.run,
            kill=os.kill, clock=time.monotonic, sleep=time.sleep, tmpdir: str | None = None) -> dict:
    """Start `app` through LaunchServices with `args` (OUT replaced by a new file's path), and return its
    answer, or end the copy it started when no answer came within `timeout` seconds. See the module."""
    if not any(OUT in a for a in args):
        raise ValueError(f"args must name the answer file as {OUT!r}")
    fd, out = tempfile.mkstemp(prefix="claude-human-answer-", dir=tmpdir)
    os.close(fd)
    os.unlink(out)   # the app writes it; an empty file left here would read as an empty answer
    argv = [a.replace(OUT, out) for a in args]
    result = {"answer": "", "outcome": "no answer", "pid": None, "ended": False, "said": ""}
    start = clock()
    try:
        try:
            r = run([OPEN, "-W", "-n", "-a", app, "--args", *argv], capture_output=True, text=True,
                    timeout=timeout)
            result["said"] = ((r.stderr or "") + (r.stdout or "")).strip()[:300]
        except subprocess.TimeoutExpired:
            result["said"] = f"open did not return within {timeout:g} s"
        except OSError as e:
            result["said"] = f"open could not run: {e}"
        while True:
            text = _read(out)
            if text:
                result.update(answer=text, outcome="answered")
                return result
            if clock() - start >= timeout:
                break
            sleep(poll)
        for pid, state in copies_naming(out, run=run):
            result["pid"] = pid
            result["outcome"] = "suspended" if state.startswith("T") else "silent"
            try:
                kill(pid, signal.SIGKILL)   # a stopped process takes no other signal until it runs again
                result["ended"] = True
            except ProcessLookupError:
                result["ended"] = True       # it ended by itself in the meantime
            except OSError as e:
                result["said"] = (result["said"] + f"; could not end pid {pid}: {e}").strip("; ")
            break
        return result
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
