"""Install an APK on an Android phone over a weak adb link, a piece at a time, so a dropped link costs only
the piece it dropped.

`adb install` sends the whole file in one session. Over a slow mobile link, or one reached through a VPN, that
session can drop part way. adb then marks the device offline, and every later try fails at once ("failed to get
feature set: device offline") until the connection is made again. Each new try also sends the whole file again.

This sends the file in pieces into a folder of its own on the phone, readable only by the shell user, and checks
each piece by its md5. The pieces stay between runs, so a second run sends only what is missing. After a piece
that did not arrive it disconnects the device and connects it again. It joins the pieces on the phone, checks the
whole file by its md5, and runs `pm install` there in the background, so the install itself does not depend on the
link either. With --package it then checks that the installed copy is the same file. On success the folder is
removed. After a failure the pieces stay for the next run, and --clear removes them.

An install on the phone can wait for a person. Google Play Protect may show a dialog about an app it has not seen
before, and the install goes on only once someone answers it on the phone. A run that finds an install of the same
file still running there waits for that one and starts no second install, and a run that ends while a dialog holds
the install names the dialog. With --package, a phone that already runs this file is a success with nothing sent.

    claude-human android install --serial HOST:PORT APK [--package NAME] [--folder /data/local/tmp/apk-install]
        [--piece-kb 4096] [--tries 4] [--piece-s 120] [--install-s 300] [--clear] [--json]

An APK can carry secrets of its own (an app built with a token in its bundle, say). The folder on the phone is
0700 and every piece 0600, and both are gone once the install succeeds or --clear runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence

ADB = "adb"
#: the folder on the phone that holds one subfolder per APK (named by its md5)
FOLDER = "/data/local/tmp/apk-install"
#: a piece small enough to arrive between two drops of a weak link (measured at 0.14 to 0.7 MB/s on mobile
#: data over a VPN: 4 MB in 6 to 30 s)
PIECE = 4 * 1024 * 1024
TRIES = 4
PIECE_S = 120.0
INSTALL_S = 300.0
EVERY = 3.0
#: how often a waiting install looks for a dialog that holds it
LOOK_S = 30.0
#: dialogs that hold an install until someone answers them on the phone: a text that names the dialog in
#: `dumpsys window`'s mFocusedApp or mCurrentFocus line, and what to call it
PROMPTS = (("protectdialogs", "a Google Play Protect dialog"),)

#: adb: run(argv, timeout=seconds) -> (exit code, what it printed on both streams)
Runner = Callable[..., tuple[int, str]]


def _runner(adb: str, env: Mapping[str, str] | None) -> Runner:
    def run(argv: Sequence[str], timeout: float = 120.0) -> tuple[int, str]:
        try:
            r = subprocess.run([adb, *argv], capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return 124, f"no answer within {timeout:.0f} s"
        except OSError as e:
            return 1, str(e)
        return r.returncode, r.stdout + r.stderr
    return run


def md5_of(path: str) -> str:
    h = hashlib.md5()   # noqa: S324 - a checksum against corruption, the same one the phone's md5sum gives
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def split(path: str, into: str, piece: int = PIECE) -> list[dict]:
    """Write `path` as pieces into the folder `into` (piece.0000, piece.0001, ..., so the shell's sorted glob
    joins them in order), each 0600, and answer [{name, path, size, md5}]."""
    out = []
    with open(path, "rb") as f:
        for i, block in enumerate(iter(lambda: f.read(piece), b"")):
            name = f"piece.{i:04d}"
            local = os.path.join(into, name)
            fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(block)
            out.append({"name": name, "path": local, "size": len(block),
                        "md5": hashlib.md5(block).hexdigest()})   # noqa: S324 - see md5_of
    return out


def md5_lines(text: str) -> dict[str, str]:
    """md5sum's output as {file name: md5}, the names without their folder."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]{32})\s+\*?(\S+)$", line.strip())
        if m:
            out[m.group(2).rsplit("/", 1)[-1]] = m.group(1)
    return out


def installed_line(text: str) -> str | None:
    """pm install's answer: "Success", or its Failure or Error line, or None while it has not answered."""
    for line in text.splitlines():
        line = line.strip()
        if line == "Success" or re.match(r"^(Failure|Error|Exception)\b", line):
            return line
    return None


def _shell(serial: str, run: Runner, command: str, timeout: float = 60.0) -> tuple[int, str]:
    return run(["-s", serial, "shell", command], timeout=timeout)


def start_command(here: str, size: int) -> str:
    """The shell line that starts `pm install` on the phone in the background and answers "started" at once.

    Only the install goes into the background, in braces and with its own input and output. A background list
    (`cd ... && rm ... && setsid ... &`) is a shell of its own that keeps adb's output open until the install ends,
    so adb answered only then, and an install that waited for a dialog read as one that never started."""
    q = shlex.quote(here)
    return (f"cd {q} && rm -f install.out && "
            f"{{ setsid sh -c 'pm install -r -S {size} < app.apk > install.out 2>&1' < /dev/null > /dev/null 2>&1 & }}"
            f" && echo started")


def running_command(here: str) -> str:
    """The shell line that prints "running <pid>" for each process of the shell user that reads `here`/app.apk as
    its input, which is what a `pm install` started by start_command does until it ends. It needs no file of its
    own, so it also finds an install that an older run started."""
    apk = shlex.quote(f"{here}/app.apk")
    return (f"for p in /proc/[0-9]*; do [ \"$(readlink $p/fd/0 2>/dev/null)\" = {apk} ] && "
            f"echo \"running ${{p#/proc/}}\"; done; true")


def running(serial: str, run: Runner, here: str) -> str | None:
    """The pid of an install of `here`/app.apk still running on the phone, or None."""
    _code, out = _shell(serial, run, running_command(here))
    pids = sorted(int(m) for m in re.findall(r"^running (\d+)$", out, re.M))
    return str(pids[0]) if pids else None


def prompt_shown(text: str) -> str | None:
    """What to call the dialog in `dumpsys window`'s focus lines that holds an install (one of PROMPTS), or None.
    The focused app counts as well as the focused window: while the phone is locked the window in focus is the
    lock screen's, and the dialog is still the app in front."""
    for line in text.splitlines():
        if "mFocusedApp" in line or "mCurrentFocus" in line:
            for key, name in PROMPTS:
                if key in line:
                    return name
    return None


def _prompt(serial: str, run: Runner) -> str | None:
    _code, out = _shell(serial, run, "dumpsys window | grep -E 'mFocusedApp|mCurrentFocus'", timeout=30)
    return prompt_shown(out)


def installed_md5(serial: str, run: Runner, package: str) -> tuple[str, str | None]:
    """(the path of `package`'s APK on the phone, its md5), with "" and None for what the phone did not say."""
    _code, out = _shell(serial, run, f"pm path {shlex.quote(package)}")
    path = next((line.strip()[len("package:"):] for line in out.splitlines()
                 if line.strip().startswith("package:")), "")
    if not path:
        return "", None
    _code, got = _shell(serial, run, f"md5sum {shlex.quote(path)}")
    return path, md5_lines(got).get(path.rsplit("/", 1)[-1])


def reconnect(serial: str, run: Runner) -> bool:
    """Drop and make again the connection to a device reached over the network (HOST:PORT): adb keeps one that
    went offline, and `adb connect` alone answers "already connected" to it. A USB serial is only asked."""
    if ":" in serial:
        run(["disconnect", serial], timeout=20)
        run(["connect", serial], timeout=30)
    code, out = run(["-s", serial, "get-state"], timeout=20)
    return code == 0 and out.strip() == "device"


def clear(serial: str, folder: str = FOLDER, *, adb: str = ADB, env: Mapping[str, str] | None = None,
          run: Runner | None = None) -> dict:
    """Remove every piece and joined file under `folder` on the phone."""
    run = run or _runner(adb, env)
    if not reconnect(serial, run):
        return {"outcome": "failure", "why": "the device does not answer over adb"}
    code, out = _shell(serial, run, f"rm -rf {shlex.quote(folder)} && echo cleared")
    ok = code == 0 and "cleared" in out
    return {"outcome": "success" if ok else "failure",
            "why": f"removed {folder}" if ok else f"could not remove {folder}: {out.strip()[-200:]}"}


def install(serial: str, apk: str, *, package: str | None = None, folder: str = FOLDER, piece: int = PIECE,
            tries: int = TRIES, piece_s: float = PIECE_S, install_s: float = INSTALL_S, every: float = EVERY,
            look_s: float = LOOK_S, adb: str = ADB, env: Mapping[str, str] | None = None, run: Runner | None = None,
            sleep=time.sleep, clock=time.monotonic, say: Callable[[str], None] = print) -> dict:
    """Send `apk` to the phone in pieces, keep what arrived, join and check it there, and install it.
    Answers {outcome: success | failure | refused, why, md5, size, pieces, sent, reused, failed_tries, waiting_for},
    where waiting_for names a dialog on the phone that holds the install, or is None."""
    if not os.path.isfile(apk):
        return {"outcome": "refused", "why": f"no file at {apk}"}
    run = run or _runner(adb, env)
    size, md5 = os.path.getsize(apk), md5_of(apk)
    here = f"{folder.rstrip('/')}/{md5}"
    q = shlex.quote(here)
    report = {"outcome": "failure", "why": "", "serial": serial, "apk": apk, "md5": md5, "size": size,
              "pieces": 0, "sent": 0, "reused": 0, "failed_tries": 0, "waiting_for": None}

    def fail(why: str) -> dict:
        report["why"] = why
        return report

    code, state = run(["-s", serial, "get-state"], timeout=20)
    if not (code == 0 and state.strip() == "device") and not reconnect(serial, run):
        return fail("the device does not answer over adb")
    if package and installed_md5(serial, run, package)[1] == md5:
        # an install that waited for someone's answer may have ended after the run that started it
        pid = running(serial, run, here)
        if not pid:
            _shell(serial, run, f"rm -rf {q}")
        report.update(outcome="success",
                      why=f"{package} already runs this file (md5 {md5[:12]}), so nothing was sent"
                          + (f"; an install of it still runs on the phone (pid {pid})" if pid else ""))
        return report
    pid = running(serial, run, here)
    if pid:
        # its pm install reads app.apk, so joining the pieces again would change the file under it
        say(f"an install of this file is still running on the phone (pid {pid}); waiting for it instead of "
            f"starting another")
    else:
        failed = _send_and_start(serial, apk, here, size, md5, piece, tries, piece_s, every, run, sleep, clock, say,
                                 report)
        if failed:
            return fail(failed)
        say("joined and checked on the phone; installing there")
    deadline, answer, prompt, look_at = clock() + install_s, None, None, clock() + look_s
    while clock() < deadline:
        sleep(every)
        code, out = _shell(serial, run, f"cat {q}/install.out 2>/dev/null")
        if code != 0:
            reconnect(serial, run)
            continue
        answer = installed_line(out)
        if answer:
            break
        if clock() >= look_at:
            look_at = clock() + look_s
            seen = _prompt(serial, run)
            if seen and seen != prompt:
                say(f"the install waits for someone to answer {seen} on the phone")
            prompt = seen
    if answer is None:
        prompt = _prompt(serial, run) or prompt
        if prompt:
            report["waiting_for"] = prompt
            return fail(f"the install waits for someone to answer {prompt} on the phone; it goes on once that is "
                        f"answered, and the file stays for the next run")
        return fail(f"the install did not answer within {install_s:.0f} s; it can still finish on the phone, and "
                    f"the file stays for the next run")
    if answer != "Success":
        return fail(f"the phone refused the install: {answer[:200]}")
    if package and installed_md5(serial, run, package)[1] != md5:
        return fail(f"{package} is installed, but its copy on the phone is not this file")
    _shell(serial, run, f"rm -rf {q}")
    report.update(outcome="success",
                  why=f"installed {os.path.basename(apk)} ({report['sent']} pieces sent, {report['reused']} already "
                      f"there){', and checked ' + package + ' by its md5' if package else ''}")
    return report


def _send_and_start(serial: str, apk: str, here: str, size: int, md5: str, piece: int, tries: int, piece_s: float,
                    every: float, run: Runner, sleep, clock, say: Callable[[str], None], report: dict) -> str | None:
    """Send what is missing of `apk` into `here` on the phone, join it there, check it, and start pm install.
    Counts into `report`, and answers why it failed, or None once the install has started."""
    q, qf = shlex.quote(here), shlex.quote(here.rsplit("/", 1)[0])
    # this APK's folder, private to the shell user; any other APK's pieces are removed first
    code, out = _shell(serial, run, f"mkdir -p {q} && chmod 700 {qf} {q} && "
                                    f"for d in {qf}/*; do [ \"$d\" = {q} ] || rm -rf \"$d\"; done; echo ready")
    if code != 0 or "ready" not in out:
        return f"could not make {here} on the phone: {out.strip()[-200:]}"
    with tempfile.TemporaryDirectory(prefix="apk-pieces-") as tmp:
        os.chmod(tmp, 0o700)
        parts = split(apk, tmp, piece)
        report["pieces"] = len(parts)
        _code, out = _shell(serial, run, f"cd {q} && md5sum piece.* 2>/dev/null")
        there = md5_lines(out)
        say(f"{len(parts)} pieces of up to {piece // 1024} KiB for {os.path.basename(apk)} ({size} bytes); "
            f"{sum(1 for p in parts if there.get(p['name']) == p['md5'])} already on the phone")
        for i, p in enumerate(parts, 1):
            if there.get(p["name"]) == p["md5"]:
                report["reused"] += 1
                continue
            for t in range(1, tries + 1):
                start = clock()
                code, out = run(["-s", serial, "push", p["path"], f"{here}/{p['name']}"], timeout=piece_s)
                _c, got = _shell(serial, run, f"md5sum {q}/{p['name']} 2>/dev/null")
                if code == 0 and md5_lines(got).get(p["name"]) == p["md5"]:
                    report["sent"] += 1
                    say(f"piece {i} of {len(parts)} sent ({p['size']} bytes, {clock() - start:.1f} s)")
                    break
                report["failed_tries"] += 1
                last = (out.strip().splitlines() or [""])[-1][:200]
                say(f"piece {i} of {len(parts)} did not arrive (try {t} of {tries}, exit {code}): {last}")
                if not reconnect(serial, run):
                    sleep(every)
            else:
                return (f"piece {i} of {len(parts)} did not arrive in {tries} tries; the "
                        f"{report['sent'] + report['reused']} pieces already there stay for the next run")
    # join on the phone and check the whole file
    code, out = _shell(serial, run, f"cd {q} && cat piece.* > app.apk && chmod 600 piece.* app.apk && "
                                    f"md5sum app.apk", timeout=120)
    if md5_lines(out).get("app.apk") != md5:
        _shell(serial, run, f"rm -f {q}/*")
        return ("the pieces joined on the phone do not make this file (md5 differs); they were removed, so the "
                "next run sends them again")
    # install there, in the background, so a link that drops now does not stop it
    code, out = _shell(serial, run, start_command(here, size))
    if "started" not in out:
        return f"could not start the install on the phone: {out.strip()[-200:]}"
    return None


def main(argv: list[str]) -> int:
    """`claude-human android install ...` (android.main hands it the arguments after "install")."""
    import argparse   # noqa: PLC0415
    p = argparse.ArgumentParser(prog="claude-human android install",
                                description="install an APK over a weak adb link, a piece at a time")
    p.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    p.add_argument("apk", nargs="?", help="the APK to install (not needed with --clear)")
    p.add_argument("--package", help="the app's package name, to check the installed copy by its md5")
    p.add_argument("--folder", default=FOLDER, help="the folder on the phone for the pieces")
    p.add_argument("--piece-kb", type=int, default=PIECE // 1024, help="the size of one piece")
    p.add_argument("--tries", type=int, default=TRIES, help="tries for each piece before giving up")
    p.add_argument("--piece-s", type=float, default=PIECE_S, help="seconds one piece may take")
    p.add_argument("--install-s", type=float, default=INSTALL_S, help="seconds the install on the phone may take")
    p.add_argument("--clear", action="store_true", help="remove the pieces kept on the phone, and install nothing")
    p.add_argument("--json", action="store_true", help="print the whole result as JSON")
    p.add_argument("--adb", default=ADB)
    a = p.parse_args(argv)
    if a.clear:
        result = clear(a.serial, a.folder, adb=a.adb)
    elif not a.apk:
        p.error("an APK is needed, or --clear")
    else:
        result = install(a.serial, a.apk, package=a.package, folder=a.folder, piece=a.piece_kb * 1024,
                         tries=a.tries, piece_s=a.piece_s, install_s=a.install_s, adb=a.adb,
                         say=(lambda _s: None) if a.json else print)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(result["why"] if result["outcome"] == "success" else f"NOT INSTALLED: {result['why']}")
    return {"success": 0, "refused": 2}.get(result["outcome"], 1)
