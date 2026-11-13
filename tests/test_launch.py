import signal
import subprocess

import pytest

from claude_human import launch


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def fake_run(*, answer=None, copy_state=None, open_error=None, extra_ps=""):
    """A run() for ask_app: `open` writes `answer` to the --out file when given, and `ps` lists one copy
    naming that file in `copy_state` when given, beside `extra_ps`."""
    seen = {"argv": None}

    def run(argv, **kw):
        if argv[0] == launch.OPEN:
            seen["argv"] = argv
            if open_error:
                raise open_error
            out = argv[argv.index("--out") + 1]
            if answer is not None:
                with open(out, "w") as f:
                    f.write(answer)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == launch.PS:
            out = seen["argv"][seen["argv"].index("--out") + 1]
            lines = extra_ps
            if copy_state:
                lines += f"  4242 {copy_state}    /Apps/Helper.app/Contents/MacOS/helper status --out {out} --wait 5\n"
            return subprocess.CompletedProcess(argv, 0, lines, "")
        raise AssertionError(argv)
    return run, seen


def test_an_answer_is_returned_and_nothing_is_ended(tmp_path):
    run, seen = fake_run(answer="authorized\n", copy_state="T")
    killed = []
    r = launch.ask_app("/Apps/Helper.app", ["status", "--out", launch.OUT], run=run,
                       kill=lambda *a: killed.append(a), tmpdir=str(tmp_path))
    assert r == {"answer": "authorized", "outcome": "answered", "pid": None, "ended": False, "said": ""}
    assert killed == []
    assert seen["argv"][:5] == [launch.OPEN, "-W", "-n", "-a", "/Apps/Helper.app"]
    assert list(tmp_path.iterdir()) == []   # the answer file is removed


def test_a_suspended_copy_is_ended_by_its_pid_after_the_deadline(tmp_path):
    clock = Clock()
    run, _ = fake_run(copy_state="T")
    killed = []
    r = launch.ask_app("/Apps/Helper.app", ["status", "--out", launch.OUT], timeout=30, run=run,
                       kill=lambda *a: killed.append(a), clock=clock, sleep=clock.sleep, tmpdir=str(tmp_path))
    assert r["outcome"] == "suspended" and r["pid"] == 4242 and r["ended"] is True
    assert killed == [(4242, signal.SIGKILL)]
    assert clock.t >= 30


def test_a_running_copy_that_wrote_nothing_is_silent_and_ended(tmp_path):
    clock = Clock()
    run, _ = fake_run(copy_state="S")
    killed = []
    r = launch.ask_app("/Apps/Helper.app", ["status", "--out", launch.OUT], timeout=5, run=run,
                       kill=lambda *a: killed.append(a), clock=clock, sleep=clock.sleep, tmpdir=str(tmp_path))
    assert r["outcome"] == "silent" and killed == [(4242, signal.SIGKILL)]


def test_another_process_is_never_ended(tmp_path):
    clock = Clock()
    other = "  7777 T    /Apps/Helper.app/Contents/MacOS/helper status --out /tmp/someone-else --wait 5\n"
    run, _ = fake_run(extra_ps=other)
    killed = []
    r = launch.ask_app("/Apps/Helper.app", ["status", "--out", launch.OUT], timeout=2, run=run,
                       kill=lambda *a: killed.append(a), clock=clock, sleep=clock.sleep, tmpdir=str(tmp_path))
    assert r["outcome"] == "no answer" and r["pid"] is None and killed == []


def test_a_copy_gone_before_its_end_counts_as_ended(tmp_path):
    clock = Clock()
    run, _ = fake_run(copy_state="T")

    def gone(pid, sig):
        raise ProcessLookupError(pid)
    r = launch.ask_app("/Apps/Helper.app", ["status", "--out", launch.OUT], timeout=1, run=run, kill=gone,
                       clock=clock, sleep=clock.sleep, tmpdir=str(tmp_path))
    assert r["ended"] is True and r["pid"] == 4242


def test_open_that_cannot_run_is_said(tmp_path):
    clock = Clock()
    run, _ = fake_run(open_error=FileNotFoundError("no open"))
    r = launch.ask_app("/Apps/Helper.app", ["status", "--out", launch.OUT], timeout=1, run=run,
                       kill=lambda *a: None, clock=clock, sleep=clock.sleep, tmpdir=str(tmp_path))
    assert r["outcome"] == "no answer" and "open could not run" in r["said"]


def test_args_must_name_the_answer_file():
    with pytest.raises(ValueError):
        launch.ask_app("/Apps/Helper.app", ["status"])


def test_copies_naming_matches_a_whole_argument_only():
    ps = ("  10 T    /x/helper --out /tmp/a-1\n"
          "  11 S    /x/helper --out /tmp/a-12\n"
          "  bad line\n")
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, ps, "")
    assert launch.copies_naming("/tmp/a-1", run=run) == [(10, "T")]
