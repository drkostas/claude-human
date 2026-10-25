"""One password-manager CLI session at a time on a Mac, across every process that uses the lock.

Some password-manager CLIs keep one session for the whole user account. Bitwarden's `bw` is one: a
second `bw unlock` replaces the key the first session holds, and `bw lock` ends every session. Two
programs that each unlock, work and lock again therefore break each other. The one that started
first finds its next call answered with "Vault is locked." and stops halfway through its work.

`SessionLock` keeps such a CLI to one session at a time.

- It is an exclusive `flock` on a state file. The kernel lets go of a flock with the last
  descriptor of the process that holds it, so a holder that dies, even by SIGKILL, never blocks the
  next one.
- While it is held, the file names the holder as one line of JSON, with its pid, the act it serves,
  the program and since when. A refusal after a wait names that holder, so the person reading it
  knows what to wait for.
- A second take in the thread that already holds the lock is refused at once. A second unlock
  would end the session the thread is using.
- Waiters are served in the order they came, through a queue file beside the lock. A waiter that
  has not said for `stale_s` seconds that it still waits (its process was killed or stopped) leaves
  the queue, so it never holds the line.
- `run(argv)` runs a CLI call while this thread holds the lock. It first checks that the lock file
  is still the one this thread locked (a lock file removed while held would let another process
  make a new one and unlock), and it names the call in the lock while it runs. A holder killed in
  the middle of a call leaves its child running, because a child outlives its parent. The next
  holder waits for that call until its own timeout would have ended it, and then ends it.
- `hold(act, wait_s)` is a context manager that takes the lock or raises `Busy`, and lets go of it
  on every way out, an exception included.

`bw_session(lock, password)` is the whole cycle for Bitwarden's `bw`. It takes the lock, unlocks
with the password through a pipe that `bw` reads as `--passwordfile /dev/fd/<n>` (so the password
is never in an argument, an environment variable or a file), yields the environment that carries
`BW_SESSION`, and on the way out runs `bw lock` and lets go of the lock.

The lock path, the queue path, the CLI and the messages are all arguments, so the same lock works
for any CLI with one session per account.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path

from .process import process_alive

#: the keys of the record that name the CLI call a holder runs now
_CHILD_KEYS = ("child_pid", "child_started", "child_until")
#: a password is short, and one longer than this is not the one meant; writing it whole into the
#: pipe before the CLI starts must not wait for a reader
PASSWORD_MOST = 4096


class Busy(RuntimeError):
    """The lock was not taken. The message says why, the holder first when another act holds it."""


class SessionError(RuntimeError):
    """The CLI session did not open (a wrong password, a CLI that did not answer)."""


class Held:
    """The lock one thread holds: the descriptor whose flock it is, the thread, and the record the
    lock names it by."""
    __slots__ = ("fd", "thread", "record", "lock")

    def __init__(self, fd: int, thread: int, record: dict, lock: SessionLock):
        self.fd, self.thread, self.record, self.lock = fd, thread, record, lock


#: every lock held in this process, by (lock path, thread), from the moment it is taken
_TAKEN: dict[tuple[str, int], Held] = {}
_GUARD = threading.Lock()


def _put(fd: int, record: dict) -> None:
    """Make `record` the whole of the file, as one line of JSON. Written over the old line and then
    cut to its length, never emptied first, so a reader in between finds a whole first line."""
    data = (json.dumps(record) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)
    os.ftruncate(fd, len(data))


def _read(fd: int) -> dict | None:
    try:
        rec = json.loads(os.pread(fd, 1 << 16, 0).decode("utf-8").split("\n", 1)[0] or "null")
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def waited_words(seconds: float) -> str:
    """A wait in words: 15 minutes, 90 seconds, 1 second."""
    if seconds >= 120:
        return f"{seconds / 60:g} minutes"
    return f"{seconds:g} second{'' if seconds == 1 else 's'}"


def _said_when(then: datetime, now: datetime) -> str:
    if then.date() == now.date():
        return then.strftime("%H:%M:%S")
    return then.strftime("%Y-%m-%d %H:%M:%S")


class SessionLock:
    """A cross-process lock that keeps a CLI to one session at a time. See the module's text.

    `path` is the lock file (its folder is made with mode 0700). `queue` is the waiters' file,
    by default `<path>.queue` beside it. `label` names what is locked in messages. `env` is the
    environment for the CLI calls `run` makes when the caller passes none, and for `ps`. `now`
    gives the time the record says since, and `said_when(then, now)` writes it in a refusal.

    The messages are format strings on the class, so a program may set its own wording on an
    instance: BUSY ({label}, {holder}, {waited}), AGAIN ({label}), LOST ({label}) and NOT_OPENED
    ({label}, {why})."""

    BUSY = ("{label} is busy with {holder}, which holds the one session, and this act waited "
            "{waited} for it, so try again once it ends")
    AGAIN = "this act already holds {label}, and a second unlock would end it"
    LOST = ("the lock on {label} was removed while this act held it, so another act could start a "
            "session, and this act stopped before its next call")
    NOT_OPENED = "the lock on {label} could not be opened ({why})"

    def __init__(self, path: str | Path, *, queue: str | Path | None = None,
                 label: str = "the CLI session", poll_s: float = 0.5, stale_s: float = 10.0,
                 orphan_grace_s: float = 5.0, child_timeout_s: float = 120.0,
                 env: Mapping[str, str] | None = None,
                 now: Callable[[], datetime] | None = None,
                 said_when: Callable[[datetime, datetime], str] | None = None):
        self.path = Path(path).expanduser()
        self.queue = Path(queue).expanduser() if queue else self.path.with_name(self.path.name + ".queue")
        self.label = label
        self.poll_s, self.stale_s = poll_s, stale_s
        self.orphan_grace_s, self.child_timeout_s = orphan_grace_s, child_timeout_s
        self.env = env
        self.now = now or (lambda: datetime.now().astimezone())
        self.said_when = said_when or _said_when

    # ------------------------------------------------------------------ who holds it

    def _key(self) -> tuple[str, int]:
        return (str(self.path.absolute()), threading.get_ident())

    def mine(self) -> Held | None:
        """The lock this thread holds, from the moment it took it."""
        with _GUARD:
            return _TAKEN.get(self._key())

    def holder(self) -> dict | None:
        """The record of the act that holds the lock now, read without taking it, or None."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            rec = _read(fd)
        finally:
            os.close(fd)
        return rec if rec and rec.get("pid") else None

    def holder_words(self, rec: dict | None) -> str:
        """Who holds the lock, from its record: the act, its pid and program, and since when."""
        if not rec or not rec.get("pid"):
            return "an act that has not written its name into the lock yet"
        try:
            since = self.said_when(datetime.fromisoformat(rec["since"]), self.now())
        except (KeyError, TypeError, ValueError):
            since = "a time it did not write"
        return (f"{str(rec.get('act') or 'an act')[:120]} (pid {rec['pid']} in "
                f"{str(rec.get('program') or 'a process')[:60]}, since {since})")

    def _same_file(self, fd: int) -> bool:
        """Whether the file `fd` locked is still the one at the path."""
        try:
            a, b = os.fstat(fd), os.stat(self.path, follow_symlinks=False)
        except OSError:
            return False
        return a.st_nlink > 0 and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)

    def lost(self, held: Held) -> str:
        """Why `held` no longer keeps the CLI to one session (its file was removed or replaced),
        or "" while it does."""
        return "" if self._same_file(held.fd) else self.LOST.format(label=self.label)

    # ------------------------------------------------------------------ the waiters' queue

    def _queue(self, change):
        """Read the queue ({next, waiting: [{n, pid, act, beat}]}), let `change` change it and write
        it back, under a flock of its own held for that read and write only."""
        fd = os.open(self.queue, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            q, now = _read(fd) or {}, time.time()

            def fresh(w) -> bool:
                try:
                    return isinstance(w["n"], int) and now - float(w["beat"]) < self.stale_s
                except (KeyError, TypeError, ValueError):
                    return False
            nxt = q.get("next")
            q = {"next": nxt if isinstance(nxt, int) else 0,
                 "waiting": [w for w in q.get("waiting") or [] if fresh(w)]}
            out = change(q)
            _put(fd, q)
            return out
        finally:
            os.close(fd)

    def _join(self, act: str) -> int:
        def join(q):
            n = q["next"]
            q["next"] = n + 1
            q["waiting"].append({"n": n, "pid": os.getpid(), "act": act[:120], "beat": time.time()})
            return n
        return self._queue(join)

    def _first(self, ticket: int, act: str) -> bool:
        """Whether `ticket` is the oldest waiter, after saying it still waits. A waiter dropped as
        gone (a Mac asleep longer than stale_s) takes its own place again."""
        def turn(q):
            me = next((w for w in q["waiting"] if w["n"] == ticket), None)
            if me is None:
                me = {"n": ticket, "pid": os.getpid(), "act": act[:120]}
                q["waiting"].append(me)
                q["waiting"].sort(key=lambda w: w["n"])
            me["beat"] = time.time()
            return q["waiting"][0]["n"] == ticket
        try:
            return self._queue(turn)
        except OSError:
            # a queue that cannot be read loses the order, never the lock: the flock still decides
            return True

    def _leave(self, ticket: int) -> None:
        def leave(q):
            q["waiting"] = [w for w in q["waiting"] if w["n"] != ticket]
        with contextlib.suppress(OSError):
            self._queue(leave)

    # ------------------------------------------------------------------ a dead holder's CLI call

    def _started_at(self, pid: int) -> tuple[float, ...]:
        """When the process `pid` started, to the second, as every epoch its local start time can
        mean: `ps -o lstart` prints a local time, and on the night the clocks go back the hour
        between the two changes happens twice. mktime picks one of the two, an hour off for a
        process started in the other, so both are returned and the caller matches either. Empty
        when ps did not answer."""
        env = dict(self.env if self.env is not None else os.environ, LC_ALL="C")
        try:
            r = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                               text=True, env=env, timeout=5)
            tm = time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y")
        except (OSError, subprocess.SubprocessError, ValueError):
            return ()
        out: list[float] = []
        for isdst in (1, 0):
            try:
                out.append(time.mktime(time.struct_time(tm[:8] + (isdst,))))
            except (OverflowError, ValueError):
                continue
        return tuple(dict.fromkeys(out))

    def _still_running(self, pid: int, started: float) -> bool:
        """Whether `pid` is the process that started at `started` and has not ended. A pid used
        again by another process is not it."""
        if process_alive(pid) is not True:
            return False
        return any(abs(at - started) <= 2 for at in self._started_at(pid))

    def _orphan(self, prev: dict | None) -> None:
        """Wait for, and at its timeout end, a CLI call the lock's last holder left running."""
        pid, started, until = ((prev or {}).get(k) for k in _CHILD_KEYS)
        if not isinstance(pid, int) or pid <= 0 or not isinstance(started, (int, float)):
            return
        until = (until if isinstance(until, (int, float)) else started + self.child_timeout_s) \
            + self.orphan_grace_s
        while self._still_running(pid, started) and time.time() < until:
            time.sleep(self.poll_s)
        if self._still_running(pid, started):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
            end = time.time() + 5
            while self._still_running(pid, started) and time.time() < end:
                time.sleep(self.poll_s)

    # ------------------------------------------------------------------ take and release

    def take(self, act: str, wait_s: float) -> tuple[Held | None, str]:
        """(the held lock, "") once this act holds the one session, or (None, why). Refused at once
        when this thread holds it already. Waits up to `wait_s` seconds for the acts ahead of it,
        served in the order they came, and then refuses with the holder named."""
        me = threading.get_ident()
        with _GUARD:
            if self._key() in _TAKEN:
                return None, self.AGAIN.format(label=self.label)
        deadline = time.monotonic() + wait_s
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ticket = self._join(act)
        except OSError as e:
            return None, self.NOT_OPENED.format(label=self.label, why=e.strerror or type(e).__name__)
        try:
            while True:
                try:
                    fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                except OSError as e:
                    return None, self.NOT_OPENED.format(label=self.label,
                                                        why=e.strerror or type(e).__name__)
                try:
                    while True:
                        if self._first(ticket, act):
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                pass
                        if time.monotonic() >= deadline:
                            return None, self.BUSY.format(label=self.label,
                                                          holder=self.holder_words(_read(fd)),
                                                          waited=waited_words(wait_s))
                        time.sleep(self.poll_s)
                    if self._same_file(fd):
                        prev = _read(fd)
                        record = {"pid": os.getpid(), "act": act[:120],
                                  "program": os.path.basename((getattr(sys, "argv", None) or [""])[0])
                                  or "python",
                                  "since": self.now().isoformat(timespec="seconds")}
                        with contextlib.suppress(OSError):
                            _put(fd, record)   # a record not written leaves the lock held all the same
                        self._orphan(prev)
                        held, fd = Held(fd, me, record, self), -1
                        with _GUARD:
                            _TAKEN[self._key()] = held
                        return held, ""
                except OSError as e:
                    return None, self.NOT_OPENED.format(label=self.label,
                                                        why=e.strerror or type(e).__name__)
                finally:
                    if fd >= 0:
                        os.close(fd)
                # the file was removed or replaced while this act waited: lock the one there now
        finally:
            self._leave(ticket)

    def release(self, held: Held | None) -> None:
        """Let go of the lock: its record first, so the file never names an act that has ended,
        then the flock and its descriptor. Safe to call with None or twice."""
        if held is None:
            return
        with _GUARD:
            key = (str(self.path.absolute()), held.thread)
            if _TAKEN.get(key) is held:
                del _TAKEN[key]
        if held.fd < 0:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(held.fd, 0)
        with contextlib.suppress(OSError):
            fcntl.flock(held.fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(held.fd)
        held.fd = -1

    @contextlib.contextmanager
    def hold(self, act: str, wait_s: float) -> Iterator[Held]:
        """Take the lock or raise `Busy`, and let go of it on every way out."""
        held, why = self.take(act, wait_s)
        if held is None:
            raise Busy(why)
        try:
            yield held
        finally:
            self.release(held)

    # ------------------------------------------------------------------ CLI calls

    def _child(self, held: Held, pid: int | None, timeout: float | None = None) -> None:
        if pid is None:
            for k in _CHILD_KEYS:
                held.record.pop(k, None)
        else:
            now = time.time()
            held.record.update(child_pid=pid, child_started=now,
                               child_until=now + (timeout or self.child_timeout_s))
        with contextlib.suppress(OSError):
            _put(held.fd, held.record)

    def run(self, argv: list[str], *, env: Mapping[str, str] | None = None, input: str | None = None,
            timeout: float | None = None, even_if_lost: bool = False,
            **kw) -> subprocess.CompletedProcess:
        """Run a CLI call, text in and out, like `subprocess.run(capture_output=True, text=True)`.

        While this thread holds the lock, the call is refused (exit 1, the reason on stderr) when
        the lock was lost, unless `even_if_lost` (for the call that ends the session, which must
        always run). The lock names the call while it runs. A call that runs out of time or is
        interrupted ends its process and raises, as `subprocess.run` does."""
        env = env if env is not None else self.env
        env = None if env is None else dict(env)
        held = self.mine()
        if held is None:
            return subprocess.run(argv, capture_output=True, text=True, env=env, input=input,
                                  timeout=timeout, **kw)
        if not even_if_lost:
            why = self.lost(held)
            if why:
                return subprocess.CompletedProcess(argv, 1, "", why)
        with subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                              **kw) as p:
            self._child(held, p.pid, timeout)
            try:
                out, err = p.communicate(input, timeout=timeout)
            except BaseException:
                p.kill()
                p.communicate()
                raise
            finally:
                self._child(held, None)
        return subprocess.CompletedProcess(argv, p.returncode, out, err)


@contextlib.contextmanager
def bw_session(lock: SessionLock, password: Callable[[], str], *, act: str, wait_s: float = 30.0,
               bw: str = "bw", env: Mapping[str, str] | None = None,
               timeout: float = 60) -> Iterator[dict]:
    """One Bitwarden CLI session under `lock`: yields the environment with `BW_SESSION` set.

    The lock is taken first (`Busy` when it is not), and only then is `password()` called. The
    password goes to `bw unlock --passwordfile /dev/fd/<n> --raw` through a pipe, written whole and
    closed before bw starts. Every call runs with `BW_NOINTERACTION=true`, so a session that ended
    under a call answers "Vault is locked." instead of asking for the password on stdin. On the way
    out, an exception included, `bw lock` runs (it makes the session key useless) and the lock is
    let go. A session that does not open raises `SessionError` and lets go of the lock at once.

    Run the calls inside the block with `lock.run(argv, env=session_env)`, so each is checked
    against the lock and named in it."""
    base = dict(env if env is not None else (lock.env if lock.env is not None else os.environ))
    base["BW_NOINTERACTION"] = "true"
    base.pop("BW_SESSION", None)
    with lock.hold(act, wait_s):
        data = (password() + "\n").encode()
        if len(data) > PASSWORD_MOST:
            raise SessionError("the password is far longer than a password")
        r_fd, w_fd = os.pipe()
        try:
            sent = 0
            while sent < len(data):
                sent += os.write(w_fd, data[sent:])
            os.close(w_fd)
            w_fd = -1
            try:
                r = lock.run([bw, "unlock", "--passwordfile", f"/dev/fd/{r_fd}", "--raw"],
                             env=base, timeout=timeout, pass_fds=(r_fd,))
            except (OSError, subprocess.SubprocessError) as e:
                why = (f"gave no answer in {e.timeout:g} seconds"
                       if isinstance(e, subprocess.TimeoutExpired) else type(e).__name__)
                raise SessionError(f"{bw} unlock failed: {why}") from None
        finally:
            os.close(r_fd)
            if w_fd >= 0:
                os.close(w_fd)
        if r.returncode or not (r.stdout or "").strip():
            raise SessionError(f"{bw} unlock failed: {(r.stderr or '')[:120]}")
        session = dict(base, BW_SESSION=r.stdout.strip())
        try:
            yield session
        finally:
            with contextlib.suppress(Exception):
                lock.run([bw, "lock"], env=session, timeout=30, even_if_lost=True)
