"""One CLI session at a time: the lock between threads and processes, and a stand-in for `bw`.

No real password manager runs here. The CLI is a small script under tmp_path, and the password is
made at random for each test."""
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_human import session_lock
from claude_human.session_lock import Busy, SessionError, SessionLock

SRC = str(Path(__file__).resolve().parents[1] / "src")

#: a process that takes the lock, writes when it got it, holds it, and lets go
HOLDER = """
import sys, time, pathlib
sys.path.insert(0, {src!r})
from claude_human.session_lock import SessionLock
path, act, hold_s, out, wait_s = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4], float(sys.argv[5])
lock = SessionLock(path, poll_s=0.02)
held, why = lock.take(act, wait_s)
pathlib.Path(out).write_text(f"{{time.time()}} {{'held' if held else why}}")
if held:
    if len(sys.argv) > 6:
        lock.run(sys.argv[6:], timeout=float(hold_s))
    else:
        time.sleep(hold_s)
    lock.release(held)
    pathlib.Path(out + ".done").write_text(str(time.time()))
"""


def holder(tmp_path, path, act, hold_s, wait_s=10.0, cli=()):
    out = tmp_path / f"{act}.out"
    p = subprocess.Popen([sys.executable, "-c", HOLDER.format(src=SRC), str(path), act, str(hold_s),
                          str(out), str(wait_s), *cli])
    return p, out


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def stand_in(tmp_path, body):
    tool = tmp_path / "standin-cli"
    tool.write_text(f"#!{sys.executable}\n{body}")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return str(tool)


def test_the_lock_names_its_holder_and_says_nothing_after(tmp_path):
    lock = SessionLock(tmp_path / "state" / "cli.lock", poll_s=0.02)
    held, why = lock.take("sync.read", 1)
    assert why == "" and held is not None
    rec = lock.holder()
    assert rec["pid"] == os.getpid() and rec["act"] == "sync.read" and rec["program"] and rec["since"]
    assert oct(lock.path.parent.stat().st_mode & 0o777) == "0o700"
    lock.release(held)
    assert lock.holder() is None and lock.mine() is None
    lock.release(held)        # twice is harmless


def test_a_second_take_in_the_same_thread_is_refused_and_another_thread_waits(tmp_path):
    lock = SessionLock(tmp_path / "cli.lock", poll_s=0.02, label="the vault")
    held, _ = lock.take("first", 1)
    again, why = SessionLock(tmp_path / "cli.lock").take("second", 1)
    assert again is None and "already holds" in why

    got = {}
    t = threading.Thread(target=lambda: got.update(r=lock.take("other thread", 0.2)))
    t.start()
    t.join()
    assert got["r"][0] is None and "the vault is busy with first" in got["r"][1]
    lock.release(held)


def test_hold_lets_go_when_the_body_raises(tmp_path):
    lock = SessionLock(tmp_path / "cli.lock", poll_s=0.02)
    with pytest.raises(ValueError):
        with lock.hold("act", 1):
            assert lock.mine() is not None
            raise ValueError("stand-in")
    assert lock.mine() is None and lock.holder() is None
    held, why = lock.take("next", 0.2)
    assert held is not None
    lock.release(held)


def test_a_second_process_waits_for_the_first(tmp_path):
    path = tmp_path / "cli.lock"
    p, out = holder(tmp_path, path, "first", 1.0)
    assert wait_for(out.exists)
    lock = SessionLock(path, poll_s=0.02)
    held, why = lock.take("second", 10)
    got = time.time()
    p.wait(10)
    assert held is not None and why == ""
    assert got >= float(Path(str(out) + ".done").read_text()) - 0.05
    lock.release(held)


def test_a_wait_that_runs_out_names_the_holder(tmp_path):
    path = tmp_path / "cli.lock"
    p, out = holder(tmp_path, path, "nightly.store", 5.0)
    try:
        assert wait_for(out.exists)
        held, why = SessionLock(path, poll_s=0.02).take("ask", 0.3)
        assert held is None
        assert why.startswith("the CLI session is busy with nightly.store (pid ")
        assert str(p.pid) in why and "waited 0.3 seconds" in why
    finally:
        p.kill()
        p.wait()


def test_a_killed_holder_lets_go_with_its_process(tmp_path):
    path = tmp_path / "cli.lock"
    p, out = holder(tmp_path, path, "doomed", 60.0)
    assert wait_for(out.exists)
    p.send_signal(signal.SIGKILL)
    p.wait()
    t = time.monotonic()
    held, why = SessionLock(path, poll_s=0.02).take("next", 5)
    assert held is not None and time.monotonic() - t < 2
    SessionLock(path).release(held)


def test_a_cli_call_left_by_a_killed_holder_is_waited_for_and_then_ended(tmp_path):
    path = tmp_path / "cli.lock"
    cli = stand_in(tmp_path, "import time\ntime.sleep(60)\n")
    p, out = holder(tmp_path, path, "doomed", 1.0, cli=[cli])
    lock = SessionLock(path, poll_s=0.05, orphan_grace_s=0.2)
    assert wait_for(lambda: (lock.holder() or {}).get("child_pid"))
    child = lock.holder()["child_pid"]
    p.send_signal(signal.SIGKILL)
    p.wait()
    assert session_lock.process_alive(child) is True        # the child outlives its parent
    t = time.monotonic()
    held, why = lock.take("next", 10)
    assert held is not None
    assert 0.5 < time.monotonic() - t < 8                   # waited for the call's own timeout
    assert wait_for(lambda: session_lock.process_alive(child) is not True or _zombie(child), 5)
    lock.release(held)


def _zombie(pid):
    r = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip().startswith("Z") or not r.stdout.strip()


def test_waiters_are_served_in_the_order_they_came(tmp_path):
    path = tmp_path / "cli.lock"
    first, first_out = holder(tmp_path, path, "first", 1.0)
    assert wait_for(first_out.exists)
    second, second_out = holder(tmp_path, path, "second", 0.5)
    queue = Path(str(path) + ".queue")
    assert wait_for(lambda: queue.exists() and "second" in queue.read_text())
    lock = SessionLock(path, poll_s=0.02)
    held, why = lock.take("third", 15)
    got = time.time()
    first.wait(10)
    second.wait(10)
    assert held is not None
    assert got >= float(Path(str(second_out) + ".done").read_text()) - 0.05
    lock.release(held)


def test_a_waiter_that_went_away_does_not_hold_the_line(tmp_path):
    path = tmp_path / "cli.lock"
    Path(str(path) + ".queue").write_text(json.dumps(
        {"next": 1, "waiting": [{"n": 0, "pid": 999999, "act": "gone", "beat": 0}]}) + "\n")
    lock = SessionLock(path, poll_s=0.02)
    t = time.monotonic()
    held, _ = lock.take("now", 2)
    assert held is not None and time.monotonic() - t < 1
    lock.release(held)
    assert json.loads(Path(str(path) + ".queue").read_text())["waiting"] == []


def test_run_names_the_cli_call_in_the_lock_while_it_runs(tmp_path):
    path = tmp_path / "cli.lock"
    cli = stand_in(tmp_path, (
        "import json, os, sys\n"
        f"rec = json.loads(open({str(path)!r}).readline())\n"
        "print(json.dumps({'mine': rec.get('child_pid') == os.getpid(), 'stdin': sys.stdin.read()}))\n"))
    lock = SessionLock(path, poll_s=0.02)
    with lock.hold("act", 1):
        r = lock.run([cli], input="hello", timeout=10)
        assert json.loads(r.stdout) == {"mine": True, "stdin": "hello"}
        assert "child_pid" not in lock.holder()


def test_a_removed_lock_file_stops_the_next_call(tmp_path):
    path = tmp_path / "cli.lock"
    log = tmp_path / "calls.log"
    cli = stand_in(tmp_path, f"import sys\nopen({str(log)!r}, 'a').write(sys.argv[1] + '\\n')\n")
    lock = SessionLock(path, poll_s=0.02)
    with lock.hold("act", 1):
        path.unlink()
        r = lock.run([cli, "list"], timeout=10)
        assert r.returncode == 1 and "was removed while this act held it" in r.stderr
        assert not log.exists()
        assert lock.run([cli, "lock"], timeout=10, even_if_lost=True).returncode == 0
    assert log.read_text() == "lock\n"


STAND_IN_BW = """import os, sys
log = os.environ["STANDIN_LOG"]
with open(log, "a") as f:
    f.write(" ".join(sys.argv[1:]) + " | interaction=" + os.environ.get("BW_NOINTERACTION", "") + "\\n")
if sys.argv[1] == "unlock":
    given = open(sys.argv[3]).read().rstrip("\\n")
    if given != os.environ["STANDIN_EXPECT"]:
        print("Invalid master password.", file=sys.stderr)
        sys.exit(1)
    print("standin" + "-" + str(os.getpid()))
elif sys.argv[1] == "list":
    print(os.environ.get("BW_SESSION", ""))
"""


def bw_env(tmp_path, expect):
    return dict(os.environ, STANDIN_LOG=str(tmp_path / "bw.log"), STANDIN_EXPECT=expect)


def test_bw_session_unlocks_through_a_pipe_and_locks_on_every_way_out(tmp_path):
    bw = stand_in(tmp_path, STAND_IN_BW)
    word = secrets.token_hex(12)
    lock = SessionLock(tmp_path / "bw.lock", poll_s=0.02, env=bw_env(tmp_path, word))
    with pytest.raises(RuntimeError, match="stand-in"):
        with session_lock.bw_session(lock, lambda: word, act="store", bw=bw) as env:
            assert env["BW_SESSION"].startswith("standin-") and env["BW_NOINTERACTION"] == "true"
            assert lock.run([bw, "list"], env=env, timeout=10).stdout.strip() == env["BW_SESSION"]
            raise RuntimeError("stand-in")
    calls = (tmp_path / "bw.log").read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["unlock", "list", "lock"]
    assert calls[0].startswith("unlock --passwordfile /dev/fd/") and word not in "".join(calls)
    assert all(c.endswith("interaction=true") for c in calls)
    assert lock.mine() is None and lock.holder() is None


def test_bw_session_that_does_not_open_lets_go_of_the_lock(tmp_path):
    bw = stand_in(tmp_path, STAND_IN_BW)
    lock = SessionLock(tmp_path / "bw.lock", poll_s=0.02, env=bw_env(tmp_path, secrets.token_hex(12)))
    with pytest.raises(SessionError, match="Invalid master password"):
        with session_lock.bw_session(lock, lambda: secrets.token_hex(12), act="store", bw=bw):
            pass
    assert lock.mine() is None and lock.holder() is None


def test_bw_session_never_reads_the_password_while_another_act_holds_the_lock(tmp_path):
    bw = stand_in(tmp_path, STAND_IN_BW)
    path = tmp_path / "bw.lock"
    p, out = holder(tmp_path, path, "long.store", 5.0)
    asked = []
    try:
        assert wait_for(out.exists)
        lock = SessionLock(path, poll_s=0.02, env=bw_env(tmp_path, "unused"))
        with pytest.raises(Busy, match="long.store"):
            with session_lock.bw_session(lock, lambda: asked.append(1) or "unused", act="ask",
                                         wait_s=0.2, bw=bw):
                pass
        assert asked == [] and not (tmp_path / "bw.log").exists()
    finally:
        p.kill()
        p.wait()


def test_a_start_time_in_the_hour_the_clocks_repeat_matches_either_reading(monkeypatch, tmp_path):
    """On the night the clocks go back, a local time between the two changes happens twice.
    `ps -o lstart` prints a local time, and mktime picks one of its two readings, an hour off for a
    process started in the other, so a dead holder's running call read as some other process and the
    next act did not wait for it. Both readings are matched now."""
    lock = SessionLock(tmp_path / "x.lock", poll_s=0.01)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        tm = time.strptime("Sun Oct 25 02:30:00 2026", "%a %b %d %H:%M:%S %Y")
        summer = time.mktime(time.struct_time(tm[:8] + (1,)))      # 00:30 UTC, before the change
        winter = time.mktime(time.struct_time(tm[:8] + (0,)))      # 01:30 UTC, after it
        assert winter - summer == 3600
        monkeypatch.setattr(session_lock.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 0, "Sun Oct 25 02:30:00 2026\n", ""))
        monkeypatch.setattr(session_lock, "process_alive", lambda pid: True)
        assert set(lock._started_at(4242)) == {summer, winter}
        assert lock._still_running(4242, summer) and lock._still_running(4242, winter)
        assert not lock._still_running(4242, summer - 10) and not lock._still_running(4242, winter + 10)
        monkeypatch.setattr(session_lock.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no such process"))
        assert lock._started_at(4242) == () and not lock._still_running(4242, summer)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
