"""Facts about processes on a Mac, each read one way, and "cannot tell" kept apart from "no".

- `process_alive(pid)`: True, False, or None when it cannot be told. A process owned by another user
  answers True (the signal is refused, so the process exists).
- `peer_pid(host, port)`: the process at the other end of a local TCP connection whose far end is
  host:port, such as the client of a request a server is answering.
- `listening(sudo=False)`: every TCP port a process listens on, with the process and the address it
  listens on (`*` is every interface). None when lsof cannot run. Without `sudo` only the user's own
  processes are listed, because lsof cannot see another user's sockets, so an empty list is not
  proof that nothing listens.
- `ancestors(pid, procs)`: `pid` and every parent above it, from a process table.
- `footprint_gb(pid)`: a process's memory footprint, the figure Activity Monitor and `top` show as
  memory (phys_footprint, which counts compressed pages), from proc_pid_rusage in about a
  microsecond instead of a `top` run of about 0.65 s. Only the user's own processes; another user's
  answers None.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import subprocess


def process_alive(pid: int | None) -> bool | None:
    """True when `pid` is a live process, False when there is none, None when it cannot be told."""
    if pid is None:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # another user's process: it exists
        return True
    except (OSError, ValueError, TypeError):
        return None
    return True


def peer_pid(host: str, port: int, *, run=subprocess.run) -> int | None:
    """The process at the other end of a local TCP connection whose far end is host:port, from one
    `lsof`. None when it cannot be told (no such connection, or more than one other process)."""
    try:
        r = run(["/usr/sbin/lsof", "-nP", f"-iTCP@{host}:{int(port)}", "-sTCP:ESTABLISHED", "-Fp"],
                capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    pids = {int(line[1:]) for line in (getattr(r, "stdout", "") or "").splitlines()
            if line.startswith("p") and line[1:].isdigit()}
    pids.discard(os.getpid())
    return pids.pop() if len(pids) == 1 else None


def parse_listening(text: str) -> list[dict]:
    """lsof's field output (-F pcLn) as {pid, command, user, address, port}, one per socket."""
    out: list[dict] = []
    pid = command = user = None
    for line in text.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, command, user = (int(value) if value.isdigit() else None), None, None
        elif tag == "c":
            command = value
        elif tag == "L":
            user = value
        elif tag == "n" and ":" in value:
            address, _, port = value.rpartition(":")
            if port.isdigit():
                out.append({"pid": pid, "command": command, "user": user,
                            "address": address.strip("[]"), "port": int(port)})
    return out


def listening(*, sudo: bool = False, run=subprocess.run) -> list[dict] | None:
    """Every listening TCP socket: {pid, command, user, address, port}, address "*" for every
    interface. None when lsof (or `sudo -n`) cannot run. Without `sudo`, only this user's processes:
    lsof does not see another user's sockets, so a port missing from the list may still be open."""
    argv = ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-FpcLn"]
    if sudo:
        argv = ["/usr/bin/sudo", "-n", *argv]
    try:
        r = run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    text = getattr(r, "stdout", "") or ""
    # lsof exits 1 when it lists nothing; sudo -n exits 1 when it would ask for a password
    if getattr(r, "returncode", 0) not in (0, 1) or (sudo and r.returncode == 1 and not text):
        return None
    return parse_listening(text)


def ancestors(pid: int, procs: dict[int, dict]) -> list[int]:
    """Pure. `pid` and each parent above it, from a process table ({pid: {"ppid": ...}})."""
    out, seen = [], set()
    while pid and pid not in seen and pid in procs:
        out.append(pid)
        seen.add(pid)
        pid = procs[pid].get("ppid")
    return out


class _RusageInfoV2(ctypes.Structure):
    """libproc's rusage_info_v2 (sys/resource.h): a uuid, then 64-bit counters in this order."""
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time",
        "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups", "ri_child_pageins",
        "ri_child_elapsed_abstime", "ri_diskio_bytesread", "ri_diskio_byteswritten")]


_libproc = None


def environment_of(pid: int, names, *, run=subprocess.run) -> dict[str, str]:
    """The values of the environment variables `names` in process `pid`'s own environment, as
    one `ps -E` lists them, for the ones it has; {} when the process cannot be read.

    macOS has no /proc, so a process's environment is read from `ps -Eww`, which prints it after
    the command line. The variables are picked out by name, so a command argument that happens to
    hold an equals sign is not mistaken for one, and the last value wins when a name repeats.
    What it is for: a process started in a new session (detached) keeps the environment of the
    terminal that started it, so the pane or window it came from can still be told."""
    wanted = [str(n) for n in names if str(n)]
    if not wanted or pid is None:
        return {}
    try:
        r = run(["/bin/ps", "-Eww", "-o", "command=", "-p", str(int(pid))], capture_output=True,
                text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    text = getattr(r, "stdout", "") or ""
    out: dict[str, str] = {}
    for name in wanted:
        hits = re.findall(r"(?:^|\s)" + re.escape(name) + r"=(\S*)", text)
        if hits:
            out[name] = hits[-1]
    return out


def footprint_gb(pid: int) -> float | None:
    """A process's memory footprint in GB, or None for a process that is gone or another user's."""
    global _libproc
    if _libproc is None:
        _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
    info = _RusageInfoV2()
    if _libproc.proc_pid_rusage(int(pid), 2, ctypes.byref(info)) != 0:     # RUSAGE_INFO_V2
        return None
    return info.ri_phys_footprint / 1024 ** 3


def total_gb(pids) -> float:
    """The footprints of these processes added up, in GB; a process that is gone counts nothing."""
    return sum(gb for pid in pids if (gb := footprint_gb(pid)) is not None)
