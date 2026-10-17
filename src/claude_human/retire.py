"""Retire a macOS app so that nothing starts it again, and undo that retire step by step.

A retire is a list of steps. Each step does one thing, reads the result back, and returns a record
with its outcome and the data its undo needs. The records are plain JSON, so a program can keep
them and undo the retire later, even from another process.

- `disable_service(name, label, domain)`: `launchctl disable`, so launchd does not load the service
  at boot or login.
- `unload_service(name, label, plist, domain)`: `launchctl bootout`, so launchd stops it now.
- `disable_extension(name, bundle_id)`: `pluginkit -e ignore`, so Finder (or another host app)
  stops loading an app extension.
- `quit_processes(name, find)`: every process `find` lists, by signal, TERM and then KILL after a
  grace period, read back from the process list.
- `hold_app(name, app, hold, label)`: the app renamed into a dated folder on the same disk as
  `<Name>.app.held`, so neither Finder nor a launcher can start it. It is never copied and never
  deleted. A note beside it says until when it is meant to be kept.

`retire(steps, host)` runs every step in order, each in its own `try`, so one failed step never
stops the rest. An outcome is `success`, `already` (nothing to do) or `failure`.

`undo(records, host)` puts back what the retire did. The app comes back first, and every service is
enabled before any service is loaded again, because launchd refuses to load a disabled service. A
step the retire found already done was not done by this retire, so the undo leaves it as it is,
unless the caller says an earlier retire of its own did it (`done_before`). A quit is never undone
by starting the app. Its record names the step whose undo starts it again, or says that a person
starts it by hand.

Processes are quit by signal, never by AppleScript. An app asked to quit may refuse, and scripting
an app that the calling program never scripted before raises a consent prompt that only the person
at the Mac may answer. Commands in the system domain go through `sudo -n`, which refuses rather than
prompts when it cannot run without a password.

Everything this module touches goes through a `Host`: the command runner, the signal sender, the
sleep and the disk check. Tests pass stand-ins, and no real app is touched.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: how long a quit waits after TERM before it sends KILL
GRACE_S = 10.0
#: how long a held app is meant to be kept, written into the note beside it
HOLD_DAYS = 30
#: the outcomes a step may have
SUCCESS, ALREADY, FAILURE = "success", "already", "failure"


def _run(argv: list[str], env: Mapping[str, str] | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run a command and never raise: a command that could not start or ran out of time exits 1."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=None if env is None else dict(env), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 1, "", str(e))


class Host:
    """What a retire touches on the Mac, as attributes a test replaces.

    `run(argv)` returns a CompletedProcess and never raises. `kill(pid, sig, root)` sends a signal
    and says whether it was sent (a root process through sudo). `device(path)` is the disk a path
    is on. `sudo` is the prefix for system-domain commands (empty when the program already runs as
    root). `env` is the environment every command runs with (None inherits it)."""

    def __init__(self, *, run: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
                 env: Mapping[str, str] | None = None,
                 kill: Callable[[int, int, bool], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep, grace: float = GRACE_S,
                 sudo: tuple[str, ...] = ("/usr/bin/sudo", "-n"),
                 launchctl: str = "/bin/launchctl", pluginkit: str = "/usr/bin/pluginkit",
                 uid: int | None = None,
                 device: Callable[[Path], int] | None = None):
        self.env = env
        self.run = run or (lambda argv: _run(argv, self.env))
        self.kill = kill or self._kill
        self.sleep = sleep
        self.grace = grace
        self.sudo = tuple(sudo)
        self.launchctl = launchctl
        self.pluginkit = pluginkit
        self.uid = os.getuid() if uid is None else uid
        self.device = device or (lambda p: os.stat(p).st_dev)

    def as_root(self, argv: list[str]) -> subprocess.CompletedProcess:
        return self.run([*self.sudo, *argv])

    def _kill(self, pid: int, sig: int, root: bool) -> bool:
        if root:
            name = signal.Signals(sig).name[3:]
            return self.as_root(["/bin/kill", f"-{name}", str(pid)]).returncode == 0
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    def domain(self, domain: str) -> str:
        """`system`, `gui` or `user` (for this user's id), or a domain written out (`gui/501`)."""
        return f"{domain}/{self.uid}" if domain in ("gui", "user") else domain

    def launchd(self, domain: str, *args: str) -> subprocess.CompletedProcess:
        """A launchctl command, through sudo when it acts in the system domain."""
        argv = [self.launchctl, *args]
        return self.as_root(argv) if domain == "system" else self.run(argv)


# ------------------------------------------------------------------------------------ read back

def disabled(host: Host, domain: str) -> set[str] | None:
    """The labels launchctl has disabled in a domain, or None when it could not say."""
    d = host.domain(domain)
    r = host.launchd(d, "print-disabled", d)
    if r.returncode != 0:
        return None
    return {line.split('"')[1] for line in (r.stdout or "").splitlines()
            if line.strip().startswith('"') and ("=> disabled" in line or "=> true" in line)}


def loaded(host: Host, label: str, domain: str = "system") -> bool | None:
    """Whether launchd has the service loaded, or None when it could not say."""
    d = host.domain(domain)
    r = host.launchd(d, "print", f"{d}/{label}")
    if r.returncode == 0:
        return True
    text = ((r.stdout or "") + (r.stderr or "")).lower()
    if "could not find service" in text or "not found" in text or r.returncode == 113:
        return False
    return None


def extension_enabled(host: Host, bundle_id: str) -> bool | None:
    """Whether its host app may load the extension: pluginkit marks it `-` when ignored. An
    extension that is not registered is False (there is nothing to load). None when pluginkit
    could not say."""
    r = host.run([host.pluginkit, "-m", "-i", bundle_id])
    if r.returncode != 0:
        return None
    line = next((ln.strip() for ln in (r.stdout or "").splitlines() if bundle_id in ln), "")
    if not line:
        return False
    return not line.startswith("-")


def under(*prefixes: str | Path) -> Callable[[Host], list[dict] | None]:
    """A process finder for `quit_processes`: every process whose executable path starts with one
    of `prefixes` (an app's path with a trailing slash, for example), from one `ps`. It answers
    [{"pid", "user", "path"}], or None when `ps` did not answer."""
    wanted = tuple(str(p) for p in prefixes)

    def find(host: Host) -> list[dict] | None:
        r = host.run(["/bin/ps", "-axww", "-o", "pid=,user=,comm="])
        if r.returncode != 0:
            return None
        out = []
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[2].startswith(wanted):
                out.append({"pid": int(parts[0]), "user": parts[1], "path": parts[2]})
        return out
    return find


def held_copies(hold: str | Path, label: str, app_name: str) -> list[Path]:
    """Every held copy of an app under `hold`, oldest first."""
    try:
        return sorted(p / f"{app_name}.held" for p in Path(hold).glob(f"{label}-*")
                      if (p / f"{app_name}.held").exists())
    except OSError:
        return []


# ------------------------------------------------------------------------------------ the steps

@dataclass(frozen=True)
class Step:
    """One step of a retire: `do(host, now)` acts, reads back and returns the record's fields, and
    `state(host)` reads the current state without changing anything."""
    name: str
    do: Callable[[Host, datetime], dict]
    state: Callable[[Host], object]


def disable_service(name: str, label: str, domain: str = "system") -> Step:
    """Disable a launchd service, so it does not load at boot (system) or at login (gui)."""
    def do(host: Host, _now: datetime) -> dict:
        target = f"{host.domain(domain)}/{label}"
        undo = {"enable": target}
        if label in (disabled(host, domain) or set()):
            return {"outcome": ALREADY, "undo": undo}
        host.launchd(host.domain(domain), "disable", target)
        ok = label in (disabled(host, domain) or set())
        return {"outcome": SUCCESS if ok else FAILURE, "undo": undo}

    def state(host: Host):
        d = disabled(host, domain)
        return None if d is None else label in d
    return Step(name, do, state)


def unload_service(name: str, label: str, plist: str | Path, domain: str = "system") -> Step:
    """Stop a loaded launchd service (`bootout`). Its undo loads it again from `plist`."""
    def do(host: Host, _now: datetime) -> dict:
        undo = {"bootstrap": str(plist), "label": label, "domain": domain}
        if loaded(host, label, domain) is False:
            return {"outcome": ALREADY, "undo": undo}
        host.launchd(host.domain(domain), "bootout", f"{host.domain(domain)}/{label}")
        ok = loaded(host, label, domain) is False
        return {"outcome": SUCCESS if ok else FAILURE, "undo": undo}
    return Step(name, do, lambda host: loaded(host, label, domain))


def disable_extension(name: str, bundle_id: str) -> Step:
    """Tell pluginkit to ignore an app extension, so its host app stops loading it."""
    def do(host: Host, _now: datetime) -> dict:
        undo = {"pluginkit_use": bundle_id}
        if extension_enabled(host, bundle_id) is False:
            return {"outcome": ALREADY, "undo": undo}
        host.run([host.pluginkit, "-e", "ignore", "-i", bundle_id])
        ok = extension_enabled(host, bundle_id) is False
        return {"outcome": SUCCESS if ok else FAILURE, "undo": undo}
    return Step(name, do, lambda host: extension_enabled(host, bundle_id))


def quit_by_signal(host: Host, find: Callable[[Host], list[dict] | None]) -> dict:
    """Quit every process `find` lists, TERM and then KILL after the host's grace, and read back.
    `find` answers [{"pid", "user"}] (a process of user root is signalled through sudo), or None
    when it cannot tell. Returns {"outcome", "pids", "left", "killed"}."""
    found = find(host)
    if found is None:
        return {"outcome": FAILURE, "why": "the process list could not be read", "pids": []}
    if not found:
        return {"outcome": ALREADY, "pids": []}
    for p in found:
        host.kill(p["pid"], signal.SIGTERM, p.get("user") == "root")

    def still(ps: list[dict] | None) -> list[dict] | None:
        # a list that could not be read says nothing, never "they are gone"
        return None if ps is None else [p for p in found if any(q["pid"] == p["pid"] for q in ps)]

    waited, left = 0.0, found
    while waited < host.grace:
        host.sleep(0.5)
        waited += 0.5
        left = still(find(host))
        if left == []:
            break
    # KILL only a pid the list still shows: after a list that could not be read, a pid may already
    # belong to another program
    stubborn = list(left or [])
    if stubborn:
        for p in stubborn:
            host.kill(p["pid"], signal.SIGKILL, p.get("user") == "root")
        host.sleep(0.5)
        left = still(find(host))
    if left is None:
        return {"outcome": FAILURE, "why": "the process list could not be read back",
                "pids": [p["pid"] for p in found], "killed": [p["pid"] for p in stubborn]}
    return {"outcome": SUCCESS if not left else FAILURE, "pids": [p["pid"] for p in found],
            "left": [p["pid"] for p in left], "killed": [p["pid"] for p in stubborn]}


def quit_processes(name: str, find: Callable[[Host], list[dict] | None], *,
                   restarted_by: str | None = None, by_hand: str | None = None) -> Step:
    """Quit processes by signal (`quit_by_signal`). A quit has nothing to undo itself: its record
    names the step whose undo starts the processes again (`restarted_by`), or what a person does
    to start them (`by_hand`)."""
    if restarted_by:
        undo = {"restarted_by": restarted_by}
    else:
        undo = {"by_hand": by_hand or "a person starts the app again by hand"}

    def do(host: Host, _now: datetime) -> dict:
        return {**quit_by_signal(host, find), "undo": dict(undo)}

    def state(host: Host):
        ps = find(host)
        return None if ps is None else [p["pid"] for p in ps]
    return Step(name, do, state)


def hold_app(name: str, app: str | Path, hold: str | Path, label: str, *,
             hold_days: int = HOLD_DAYS, note_file: str | None = "HELD.txt") -> Step:
    """Rename `app` into `<hold>/<label>-<local date>/<Name>.app.held`. A rename on the same disk,
    never a copy and a delete, so the app is whole until the undo renames it back. A hold folder on
    another disk is refused. `note_file` (None for none) is written beside it and says until when
    the copy is meant to be kept. An app that is gone while a held copy exists is `already`."""
    app, hold = Path(app), Path(hold)

    def do(host: Host, now: datetime) -> dict:
        day = now.astimezone().date().isoformat()
        until = now + timedelta(days=hold_days)
        place = hold / f"{label}-{day}" / f"{app.name}.held"
        if not app.exists():
            copies = held_copies(hold, label, app.name)
            if copies:
                return {"outcome": ALREADY, "held": str(copies[-1]),
                        "undo": {"move_back": str(copies[-1]), "to": str(app)}}
            return {"outcome": FAILURE, "why": f"{app} is not there and no held copy is", "undo": None}
        place.parent.mkdir(parents=True, exist_ok=True)
        if host.device(app) != host.device(place.parent):
            return {"outcome": FAILURE, "why": f"{place.parent} is on another disk than {app}",
                    "undo": None}
        if place.exists():
            return {"outcome": FAILURE, "why": f"{place} is there already", "undo": None}
        if note_file:
            (place.parent / note_file).write_text(
                f"{app.name}, held aside on {day} and meant to be kept until "
                f"{until.astimezone().date().isoformat()}. The undo of the retire renames it back to "
                f"{app}.\n")
        os.rename(app, place)
        ok = place.exists() and not app.exists()
        return {"outcome": SUCCESS if ok else FAILURE, "held": str(place),
                "held_until": until.isoformat(),
                "undo": {"move_back": str(place), "to": str(app)}}

    def state(_host: Host):
        return {"app_present": app.exists(), "held": [str(p) for p in held_copies(hold, label, app.name)]}
    return Step(name, do, state)


# ------------------------------------------------------------------------------------ run and undo

def retire(steps: Iterable[Step], host: Host | None = None, now: datetime | None = None) -> list[dict]:
    """Run every step in order and return their records, [{"step", "outcome", "undo", ...}]. A step
    that raises is recorded as a failure with the reason, and the rest still run."""
    host = host or Host()
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    for s in steps:
        try:
            out.append({"step": s.name, **s.do(host, now)})
        except Exception as e:  # noqa: BLE001 - one failed step is recorded and the rest still run
            out.append({"step": s.name, "outcome": FAILURE,
                        "why": f"{type(e).__name__}: {str(e)[:160]}", "undo": None})
    return out


def state(steps: Iterable[Step], host: Host | None = None) -> dict:
    """What each step would find now, read only: {step name: its state}."""
    host = host or Host()
    out = {}
    for s in steps:
        try:
            out[s.name] = s.state(host)
        except Exception as e:  # noqa: BLE001
            out[s.name] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def undo_order(records: list[dict]) -> list[str]:
    """The order an undo puts steps back in: newest first, with every load (`bootstrap`) after
    every enable, since launchd does not load a disabled service."""
    names = [r.get("step") for r in reversed(records)]
    loads = {r.get("step") for r in records if "bootstrap" in (r.get("undo") or {})}
    return [n for n in names if n not in loads] + [n for n in names if n in loads]


def undo(records: list[dict], host: Host | None = None, *,
         done_before: Iterable[str] = (), order: list[str] | None = None) -> list[dict]:
    """Undo every step of a retire from its records, in `order` (by default `undo_order`).

    A step recorded as `already` is left as it is and said, unless its name is in `done_before`:
    the steps an earlier retire of the caller's own did and no undo has put back since. A step that
    failed or has no undo is skipped. Each undo is read back. Returns [{"step", "outcome", ...}]
    with outcome success, already, failure, left or nothing (a quit)."""
    host = host or Host()
    done_before = set(done_before)
    rank = {n: i for i, n in enumerate(order or undo_order(records))}
    out: list[dict] = []
    for rec in sorted(records, key=lambda r: rank.get(r.get("step"), -1)):
        u = rec.get("undo") or {}
        if rec.get("outcome") not in (SUCCESS, ALREADY) or not u:
            continue
        name = rec.get("step")
        if rec["outcome"] == ALREADY and name not in done_before:
            out.append({"step": name, "outcome": "left",
                        "why": "it was already so before the retire, so the undo leaves it"})
            continue
        try:
            out.append({"step": name, **_undo_one(u, host)})
        except Exception as e:  # noqa: BLE001
            out.append({"step": name, "outcome": FAILURE, "why": f"{type(e).__name__}: {str(e)[:160]}"})
    return out


def _undo_one(u: dict, host: Host) -> dict:
    if "move_back" in u:
        src, dst = Path(u["move_back"]), Path(u["to"])
        if dst.exists():
            return {"outcome": FAILURE, "why": f"{dst} is there already"}
        if not src.exists():
            return {"outcome": FAILURE, "why": f"{src} is not there"}
        os.rename(src, dst)
        return {"outcome": SUCCESS if dst.exists() else FAILURE}
    if "enable" in u:
        domain, label = u["enable"].rsplit("/", 1)
        host.launchd(domain, "enable", u["enable"])
        # None is "could not say", and an empty set is "nothing disabled": never `or` them
        dis = disabled(host, domain)
        return {"outcome": SUCCESS if dis is not None and label not in dis else FAILURE}
    if "bootstrap" in u:
        plist = u["bootstrap"]
        # a record without a label names the service by its plist (label.plist)
        label = u.get("label") or Path(plist).stem
        domain = host.domain(u.get("domain") or "system")
        if loaded(host, label, domain) is True:
            return {"outcome": ALREADY}
        host.launchd(domain, "bootstrap", domain, plist)
        return {"outcome": SUCCESS if loaded(host, label, domain) else FAILURE}
    if "pluginkit_use" in u:
        host.run([host.pluginkit, "-e", "use", "-i", u["pluginkit_use"]])
        return {"outcome": SUCCESS if extension_enabled(host, u["pluginkit_use"]) else FAILURE}
    # a quit: undone by the step that starts it again, or by a person
    return {"outcome": "nothing", "why": u.get("by_hand") or f"restarted by {u.get('restarted_by')}"}
