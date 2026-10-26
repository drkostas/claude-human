"""Retiring an app and undoing it, against a stand-in launchctl, pluginkit and process list.

No real app, service or process is touched: the app is a folder under tmp_path, and every command
goes to a fake that keeps launchd's state in memory (or, in one test, to a stand-in launchctl on
disk that keeps it in a JSON file)."""
import json
import os
import signal
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_human import retire

UID = 501
DAEMON = "org.example.syncd"
AGENT = "org.example.Launcher"
EXT = "org.example.Sync.FinderExtension"
NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class FakeMac:
    """launchctl, pluginkit, ps and kill, kept in memory. Records every command it was given."""

    def __init__(self):
        self.disabled = {"system": set(), f"gui/{UID}": set()}
        self.loaded = {("system", DAEMON)}
        self.extensions = {EXT: True}
        self.procs = [{"pid": 4100, "user": "root", "kind": "daemon"},
                      {"pid": 4200, "user": "someone", "kind": "app"}]
        self.log: list[list[str]] = []
        self.ignore: set[str] = set()       # subcommands that "succeed" and change nothing
        self.stubborn: set[int] = set()     # pids that ignore TERM
        self.ps_fails = False

    def run(self, argv):
        self.log.append(list(argv))
        a = list(argv)
        if a[:2] == ["/usr/bin/sudo", "-n"]:
            a = a[2:]
        ok = subprocess.CompletedProcess(argv, 0, "", "")
        if a[0] == "/bin/launchctl":
            verb = a[1]
            if verb in self.ignore:
                return ok
            if verb == "print-disabled":
                lines = [f'\t"{label}" => disabled' for label in sorted(self.disabled[a[2]])]
                return subprocess.CompletedProcess(argv, 0, "disabled services = {\n" + "\n".join(lines) + "\n}\n", "")
            if verb in ("disable", "enable"):
                domain, label = a[2].rsplit("/", 1)
                (self.disabled[domain].add if verb == "disable" else self.disabled[domain].discard)(label)
                return ok
            if verb == "print":
                domain, label = a[2].rsplit("/", 1)
                if (domain, label) in self.loaded:
                    return ok
                return subprocess.CompletedProcess(argv, 113, "", f"Could not find service \"{label}\"")
            if verb == "bootout":
                domain, label = a[2].rsplit("/", 1)
                self.loaded.discard((domain, label))
                return ok
            if verb == "bootstrap":
                label = os.path.basename(a[3])[:-len(".plist")]
                if label in self.disabled[a[2]]:
                    return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: Input/output error")
                self.loaded.add((a[2], label))
                return ok
        if a[0] == "/usr/bin/pluginkit":
            if a[1] == "-m":
                if a[3] not in self.extensions:
                    return ok
                mark = "+" if self.extensions[a[3]] else "-"
                return subprocess.CompletedProcess(argv, 0, f"{mark}    {a[3]}(1.0)\n", "")
            if a[1] == "-e" and "pluginkit" not in self.ignore:
                self.extensions[a[4]] = a[2] == "use"
                return ok
            return ok
        if a[0] == "/bin/kill":
            self.signal(int(a[2]), getattr(signal, "SIG" + a[1][1:]))
            return ok
        return subprocess.CompletedProcess(argv, 1, "", "unknown command")

    def signal(self, pid, sig):
        if sig == signal.SIGTERM and pid in self.stubborn:
            return
        self.procs = [p for p in self.procs if p["pid"] != pid]

    def kill(self, pid, sig, root):
        self.log.append(["kill", str(pid), signal.Signals(sig).name, "root" if root else "user"])
        if root:
            return self.run(["/usr/bin/sudo", "-n", "/bin/kill", f"-{signal.Signals(sig).name[3:]}", str(pid)]).returncode == 0
        self.signal(pid, sig)
        return True

    def find(self, kind):
        def find(_host):
            return None if self.ps_fails else [p for p in self.procs if p["kind"] == kind]
        return find

    def host(self, **kw):
        return retire.Host(run=self.run, kill=self.kill, sleep=lambda s: None, uid=UID, **kw)


def make_app(tmp_path):
    app = tmp_path / "Applications" / "Example.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("<plist/>")
    return app


def steps(mac, app, hold):
    return [
        retire.disable_service("daemon_disabled", DAEMON, "system"),
        retire.unload_service("daemon_unloaded", DAEMON, f"/Library/LaunchDaemons/{DAEMON}.plist", "system"),
        retire.quit_processes("daemon_quit", mac.find("daemon"), restarted_by="daemon_unloaded"),
        retire.disable_extension("extension_disabled", EXT),
        retire.disable_service("launcher_disabled", AGENT, "gui"),
        retire.quit_processes("app_quit", mac.find("app"), by_hand="the person opens the app"),
        retire.hold_app("app_held", app, hold, "example"),
    ]


def outcomes(records):
    return {r["step"]: r["outcome"] for r in records}


def test_a_retire_does_each_step_and_reads_it_back(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    out = retire.retire(steps(mac, app, hold), mac.host(), NOW)
    assert outcomes(out) == dict.fromkeys(
        ["daemon_disabled", "daemon_unloaded", "daemon_quit", "extension_disabled",
         "launcher_disabled", "app_quit", "app_held"], "success")
    assert DAEMON in mac.disabled["system"] and AGENT in mac.disabled[f"gui/{UID}"]
    assert mac.loaded == set() and mac.extensions[EXT] is False and mac.procs == []
    day = NOW.astimezone().date().isoformat()
    held = hold / f"example-{day}" / "Example.app.held"
    assert not app.exists() and (held / "Contents" / "Info.plist").read_text() == "<plist/>"
    assert "meant to be kept until" in (held.parent / "HELD.txt").read_text()
    rec = {r["step"]: r for r in out}
    assert rec["app_held"]["undo"] == {"move_back": str(held), "to": str(app)}
    assert rec["app_held"]["held_until"].startswith("2030-02-14")
    assert rec["daemon_disabled"]["undo"] == {"enable": f"system/{DAEMON}"}
    assert rec["launcher_disabled"]["undo"] == {"enable": f"gui/{UID}/{AGENT}"}
    # the system domain and a root process go through sudo -n, the user's own domain does not
    assert ["/usr/bin/sudo", "-n", "/bin/launchctl", "disable", f"system/{DAEMON}"] in mac.log
    assert ["/bin/launchctl", "disable", f"gui/{UID}/{AGENT}"] in mac.log
    assert ["kill", "4100", "SIGTERM", "root"] in mac.log and ["kill", "4200", "SIGTERM", "user"] in mac.log
    # the records are plain JSON, so a program can keep them and undo later
    assert json.loads(json.dumps(out)) == out


def test_a_second_retire_finds_everything_already_done(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    retire.retire(steps(mac, app, hold), mac.host(), NOW)
    again = retire.retire(steps(mac, app, hold), mac.host(), NOW)
    assert set(outcomes(again).values()) == {"already"}
    held = {r["step"]: r for r in again}["app_held"]
    assert held["undo"]["move_back"].endswith("Example.app.held")


def test_a_step_that_does_not_take_is_a_failure_and_the_rest_still_run(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    mac.ignore = {"disable"}

    def boom(_host, _now):
        raise OSError("stand-in failure")
    out = retire.retire([*steps(mac, app, hold)[:3], retire.Step("odd", boom, lambda h: None),
                         *steps(mac, app, hold)[3:]], mac.host(), NOW)
    got = outcomes(out)
    assert got["daemon_disabled"] == "failure" and got["launcher_disabled"] == "failure"
    assert got["odd"] == "failure" and "stand-in failure" in {r["step"]: r for r in out}["odd"]["why"]
    assert got["daemon_unloaded"] == got["app_held"] == "success"


def test_undo_puts_everything_back_and_enables_before_it_loads(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    records = retire.retire(steps(mac, app, hold), mac.host(), NOW)
    mac.log.clear()
    out = retire.undo(records, mac.host())
    assert [r["step"] for r in out] == ["app_held", "app_quit", "launcher_disabled", "extension_disabled",
                                        "daemon_quit", "daemon_disabled", "daemon_unloaded"]
    assert outcomes(out) == {"app_held": "success", "app_quit": "nothing", "launcher_disabled": "success",
                             "extension_disabled": "success", "daemon_quit": "nothing",
                             "daemon_disabled": "success", "daemon_unloaded": "success"}
    assert app.exists() and mac.extensions[EXT] is True and ("system", DAEMON) in mac.loaded
    assert mac.disabled == {"system": set(), f"gui/{UID}": set()}
    flat = [" ".join(a) for a in mac.log]
    enable = next(i for i, a in enumerate(flat) if f"enable system/{DAEMON}" in a)
    load = next(i for i, a in enumerate(flat) if "bootstrap system" in a)
    assert enable < load
    # the quit is never undone by starting the app
    assert {r["step"]: r for r in out}["app_quit"]["why"] == "the person opens the app"


def test_undo_order_puts_every_load_last():
    records = [{"step": "a", "undo": {"enable": "system/x"}}, {"step": "b", "undo": {"bootstrap": "/p/x.plist"}},
               {"step": "c", "undo": {"move_back": "/h", "to": "/a"}}]
    assert retire.undo_order(records) == ["c", "a", "b"]


def test_undo_leaves_what_was_already_so_unless_an_earlier_retire_did_it(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    mac.disabled[f"gui/{UID}"].add(AGENT)              # the person turned the launcher off long ago
    records = retire.retire(steps(mac, app, hold), mac.host(), NOW)
    assert outcomes(records)["launcher_disabled"] == "already"
    out = {r["step"]: r for r in retire.undo(records, mac.host())}
    assert out["launcher_disabled"]["outcome"] == "left" and AGENT in mac.disabled[f"gui/{UID}"]

    mac2, app2 = FakeMac(), make_app(tmp_path / "second")
    mac2.disabled[f"gui/{UID}"].add(AGENT)
    records = retire.retire(steps(mac2, app2, tmp_path / "held2"), mac2.host(), NOW)
    out = {r["step"]: r for r in retire.undo(records, mac2.host(), done_before={"launcher_disabled"})}
    assert out["launcher_disabled"]["outcome"] == "success" and AGENT not in mac2.disabled[f"gui/{UID}"]


def test_undo_reads_a_record_without_a_label(tmp_path):
    mac = FakeMac()
    mac.loaded.clear()
    out = retire.undo([{"step": "daemon_unloaded", "outcome": "success",
                        "undo": {"bootstrap": f"/Library/LaunchDaemons/{DAEMON}.plist"}}], mac.host())
    assert out == [{"step": "daemon_unloaded", "outcome": "success"}]
    assert ("system", DAEMON) in mac.loaded


def test_undo_refuses_to_put_an_app_over_one_in_its_place(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    records = retire.retire(steps(mac, app, hold)[-1:], mac.host(), NOW)
    make_app(tmp_path)                                  # a new copy was installed meanwhile
    out = retire.undo(records, mac.host())
    assert out[0]["outcome"] == "failure" and "there already" in out[0]["why"]
    assert retire.held_copies(hold, "example", "Example.app")


def test_hold_refuses_a_folder_on_another_disk(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    host = mac.host(device=lambda p: 1 if "Applications" in str(p) else 2)
    out = retire.retire(steps(mac, app, hold)[-1:], host, NOW)
    assert out[0]["outcome"] == "failure" and "another disk" in out[0]["why"] and app.exists()


def test_quit_sends_kill_after_the_grace_and_only_to_pids_it_still_sees():
    mac = FakeMac()
    mac.stubborn = {4200}
    r = retire.quit_by_signal(mac.host(grace=1.0), mac.find("app"))
    assert r["outcome"] == "success" and r["killed"] == [4200]
    assert ["kill", "4200", "SIGKILL", "user"] in mac.log

    mac = FakeMac()
    mac.stubborn = {4200}
    calls = {"n": 0}

    def find(_host):
        calls["n"] += 1
        return [p for p in mac.procs if p["kind"] == "app"] if calls["n"] == 1 else None
    r = retire.quit_by_signal(mac.host(grace=1.0), find)
    assert r["outcome"] == "failure" and "could not be read back" in r["why"]
    assert not any(a[:3] == ["kill", "4200", "SIGKILL"] for a in mac.log)


def test_an_unreadable_process_list_is_a_failure_never_nothing_to_quit():
    mac = FakeMac()
    mac.ps_fails = True
    assert retire.quit_by_signal(mac.host(), mac.find("app"))["outcome"] == "failure"


def test_state_reads_and_changes_nothing(tmp_path):
    mac, app, hold = FakeMac(), make_app(tmp_path), tmp_path / "held"
    s = retire.state(steps(mac, app, hold), mac.host())
    assert s["daemon_disabled"] is False and s["daemon_unloaded"] is True
    assert s["extension_disabled"] is True and s["app_quit"] == [4200]
    assert s["app_held"] == {"app_present": True, "held": []}
    verbs = {a[3] if a[0] == "/usr/bin/sudo" else a[1] for a in mac.log}
    assert verbs <= {"print-disabled", "print", "-m"}


def test_under_finds_processes_by_their_path():
    out = "  101 root /Applications/Example.app/Contents/MacOS/syncd\n  102 someone /bin/zsh\n" \
          "  103 someone /Applications/Example.app/Contents/MacOS/Example\n"
    host = retire.Host(run=lambda argv: subprocess.CompletedProcess(argv, 0, out, ""))
    found = retire.under("/Applications/Example.app/")(host)
    assert [(p["pid"], p["user"]) for p in found] == [(101, "root"), (103, "someone")]
    host = retire.Host(run=lambda argv: subprocess.CompletedProcess(argv, 1, "", ""))
    assert retire.under("/Applications/Example.app/")(host) is None


STAND_IN_LAUNCHCTL = """#!{python}
import json, sys
state_file = {state!r}
with open(state_file) as f:
    s = json.load(f)
verb, args = sys.argv[1], sys.argv[2:]
if verb == "print-disabled":
    print("disabled services = {{")
    for label in s["disabled"].get(args[0], []):
        print(f'\\t"{{label}}" => disabled')
    print("}}")
elif verb in ("disable", "enable"):
    domain, label = args[0].rsplit("/", 1)
    have = set(s["disabled"].get(domain, []))
    (have.add if verb == "disable" else have.discard)(label)
    s["disabled"][domain] = sorted(have)
elif verb == "print":
    if args[0] not in s["loaded"]:
        print("Could not find service", file=sys.stderr)
        sys.exit(113)
elif verb == "bootout":
    s["loaded"].remove(args[0])
elif verb == "bootstrap":
    label = args[1].rsplit("/", 1)[-1][:-len(".plist")]
    if label in s["disabled"].get(args[0], []):
        sys.exit(5)
    s["loaded"].append(args[0] + "/" + label)
with open(state_file, "w") as f:
    json.dump(s, f)
"""


def test_a_stand_in_launchctl_on_disk(tmp_path):
    state = tmp_path / "launchd.json"
    state.write_text(json.dumps({"disabled": {}, "loaded": [f"gui/{UID}/{AGENT}"]}))
    tool = tmp_path / "launchctl"
    tool.write_text(STAND_IN_LAUNCHCTL.format(python=sys.executable, state=str(state)))
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    host = retire.Host(launchctl=str(tool), sudo=(), uid=UID, env=dict(os.environ))
    plist = f"/path/to/LaunchAgents/{AGENT}.plist"
    records = retire.retire([retire.disable_service("agent_disabled", AGENT, "gui"),
                             retire.unload_service("agent_unloaded", AGENT, plist, "gui")], host, NOW)
    assert outcomes(records) == {"agent_disabled": "success", "agent_unloaded": "success"}
    assert json.loads(state.read_text()) == {"disabled": {f"gui/{UID}": [AGENT]}, "loaded": []}
    out = retire.undo(records, host)
    assert outcomes(out) == {"agent_disabled": "success", "agent_unloaded": "success"}
    assert json.loads(state.read_text()) == {"disabled": {f"gui/{UID}": []}, "loaded": [f"gui/{UID}/{AGENT}"]}


def test_a_root_file_is_held_and_put_back_through_sudo_mv(tmp_path):
    plist = tmp_path / "LaunchDaemons" / "org.example.helper.plist"
    plist.parent.mkdir()
    plist.write_text("<plist/>")
    hold, seen = tmp_path / "held", []

    def run(argv):
        seen.append(list(argv))
        if argv[:3] == ["/usr/bin/sudo", "-n", "/bin/mv"]:
            os.rename(argv[3], argv[4])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")
    host = retire.Host(run=run, uid=UID)
    step = retire.hold_app("helper_held", plist, hold, "example", root=True)
    rec = retire.retire([step], host, NOW)[0]
    assert rec["outcome"] == "success" and not plist.exists() and Path(rec["held"]).exists()
    assert rec["undo"]["root"] is True and seen[0][:3] == ["/usr/bin/sudo", "-n", "/bin/mv"]
    out = retire.undo([rec], host)
    assert out[0]["outcome"] == "success" and plist.exists() and seen[-1][3] == rec["held"]


def test_a_root_hold_that_sudo_refuses_moves_nothing(tmp_path):
    f = tmp_path / "PrivilegedHelperTools" / "org.example.helper"
    f.parent.mkdir()
    f.write_text("x")
    host = retire.Host(run=lambda argv: subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required"),
                       uid=UID)
    rec = retire.retire([retire.hold_app("helper_held", f, tmp_path / "held", "example", root=True)],
                        host, NOW)[0]
    assert rec["outcome"] == "failure" and "password is required" in rec["why"] and f.exists()
    assert rec["undo"] is None


def test_a_plain_file_is_held_like_an_app(tmp_path):
    plist = tmp_path / "LaunchAgents" / "org.example.agent.plist"
    plist.parent.mkdir()
    plist.write_text("<plist/>")
    rec = retire.retire([retire.hold_app("agent_plist_held", plist, tmp_path / "held", "example")],
                        FakeMac().host(), NOW)[0]
    assert rec["outcome"] == "success" and not plist.exists() and "root" not in rec["undo"]
    assert retire.undo([rec], FakeMac().host())[0]["outcome"] == "success" and plist.exists()
