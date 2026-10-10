"""An env given to claude-human reaches every process it starts, and None inherits."""
import subprocess
import types

from claude_human import unlock as u
from claude_human.station import prepare
from claude_human.tasks import CommandVerifier, DefaultResolver
from claude_human.tasks.model import Task

ENV = {"PATH": "/usr/bin:/bin"}


def _capture(monkeypatch, target=subprocess):
    seen = []

    def fake(argv, **k):
        seen.append(k.get("env"))
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(target, "run", fake)
    return seen


def test_the_verifier_and_the_resolver_pass_their_env(monkeypatch):
    seen = _capture(monkeypatch)
    task = Task(id="t", capability="c", subject="s", verify=["/usr/bin/true"])
    assert CommandVerifier(env=ENV).verify(task) is True
    DefaultResolver(env=ENV).prepare({"prepare": ["/usr/bin/true"]})
    assert seen == [ENV, ENV]


def test_the_verifier_without_env_inherits(monkeypatch):
    seen = _capture(monkeypatch)
    CommandVerifier().verify(Task(id="t", capability="c", subject="s", verify=["/usr/bin/true"]))
    assert seen == [None]


def test_the_preparer_default_run_and_yabai_carry_the_env(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    yabai = tmp_path / "yabai"
    yabai.write_text("#!/bin/sh\n")
    yabai.chmod(0o755)
    screen = types.SimpleNamespace(windows=lambda: [], do_input=lambda m: {"ok": True})
    p = prepare.Preparer(screen, env={"PATH": str(tmp_path)})
    p.all_windows()
    p.sh(["/usr/bin/true"])
    assert len(seen) == 2 and all(e == {"PATH": str(tmp_path)} for e in seen)


def test_relock_passes_the_env(monkeypatch):
    seen = []
    monkeypatch.setattr(u, "_run", lambda argv, **k: seen.append(k.get("env")) or types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(u, "_sleep", lambda s: None)
    # the lock state is faked: read from the real Mac, a locked screen returned "Already locked"
    # before pmset ran, and this test failed whenever the Mac happened to be locked
    u.relock(poll_seconds=0, env=ENV, locked=lambda: False)
    assert seen and all(e is ENV for e in seen)
