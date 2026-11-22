"""Install an APK on an Android phone over a weak adb link, a piece at a time, so a dropped link costs only
the piece it dropped.

`adb install` sends the whole file in one session. Over a slow mobile link, or one reached through a VPN, that
session can drop part way. adb then marks the device offline, and every later try fails at once ("failed to get
feature set: device offline") until the connection is made again. Each new try also sends the whole file again.

This sends the file in pieces into a folder of its own on the phone, readable only by the shell user, and checks
each piece by its md5. The pieces stay between runs, so a second run sends only what is missing. After a piece
that did not arrive it disconnects the device, connects it again and reads the piece's md5 once more, because a
push can arrive whole while adb says it failed ("failed to read copy response: EOF"), or while the read right after
it gets no answer. The md5 decides, never adb's exit code. Each further try waits a little longer than the one
before, and the first piece a run sends gets more tries than the rest, since the first push after a quiet link is
the one that fails most often. On a link that is slow as well as weak, a push that has to stay up for a minute or
more is the one that fails, in every try. A piece that fails is then sent in parts that are half as big, down to
--min-part-kb, and once a push has run slowly the next pieces start in parts, so that each push stays under about
half of --slow-s seconds at the speed the last one had. The parts are checked by their md5, made into the same piece on
the phone and checked as a whole, and a part that arrived is not sent again. It joins the pieces on the phone, checks the
whole file by its md5, and runs `pm install` there in the background, so the install itself does not depend on the
link either. With --package it then checks that the installed copy is the same file. On success the folder is
removed. After a failure the pieces stay for the next run, and --clear removes them.

An install on the phone can wait for a person. Google Play Protect may show a dialog about an app it has not seen
before, and the install goes on only once someone answers it on the phone. A run that finds an install of the same
file still running there waits for that one and starts no second install, and a run that ends while a dialog holds
the install names the dialog. With --package, a phone that already runs this file is a success with nothing sent.

    claude-human android install --serial HOST:PORT APK [--package NAME] [--folder /data/local/tmp/apk-install]
        [--piece-kb 4096] [--tries 4] [--first-extra 3] [--piece-s 120] [--slow-s 60] [--min-part-kb 256]
        [--install-s 300] [--clear] [--json]

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
#: more tries for the first piece a run sends. Over a weak link its push failed far more often than the pushes after
#: it (in 3 of 7 runs, against 2 of about 70 later pieces), and a later try of it went through, while a shell command
#: just before it had answered.
FIRST_EXTRA = 3
PIECE_S = 120.0
#: a push that has to stay up longer than this is the one that fails on a slow link, so a piece the last push's speed puts above
#: it is sent in parts (see _parts_for). On a link of about 50 KB/s a 4 MiB piece needs more than a minute, and a push that long
#: can fail in every try, while a quarter of the piece needs about 20 seconds
SLOW_S = 60.0
#: a piece is never sent in parts smaller than this, and in no more than MAX_PARTS of them
MIN_PART = 256 * 1024
MAX_PARTS = 64
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


def _max_parts(size: int, min_part: int) -> int:
    """The most parts (a power of two) a piece of `size` bytes is sent in: none of them smaller than `min_part`."""
    n = 1
    while n * 2 <= MAX_PARTS and size // (n * 2) >= max(1, min_part):
        n *= 2
    return n


def _parts_for(size: int, rate: float | None, slow_s: float, min_part: int) -> int:
    """How many parts to start a piece of `size` bytes in, when the last push that arrived ran at `rate` bytes a second
    (None until one has). One, unless the whole piece is expected to take longer than `slow_s`. Then the smallest power
    of two that brings each part under half of it, within _max_parts."""
    top = _max_parts(size, min_part)
    if not rate or rate <= 0 or top == 1 or size / rate <= slow_s:
        return 1
    n = 2
    while n < top and size / rate / n > slow_s / 2:
        n *= 2
    return n


def part_name(index: int, n: int, k: int) -> str:
    """The name of part `k` of the `n` that piece `index` is sent in. It does not begin with "piece.", so the shell's
    join of the pieces and the list of what arrived never see it, until the parts are made into the piece."""
    return f"part.{index:04d}.of{n}.{k:02d}"


def cut(block: bytes, n: int) -> list[bytes]:
    """`block` in `n` parts of the same size, the last one shorter when it does not divide."""
    step = -(-len(block) // n)
    return [block[i:i + step] for i in range(0, len(block), step)] if block else [b""]


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
            tries: int = TRIES, first_extra: int = FIRST_EXTRA, piece_s: float = PIECE_S, slow_s: float = SLOW_S,
            min_part: int = MIN_PART, install_s: float = INSTALL_S, every: float = EVERY, look_s: float = LOOK_S,
            adb: str = ADB,
            env: Mapping[str, str] | None = None, run: Runner | None = None, sleep=time.sleep, clock=time.monotonic,
            say: Callable[[str], None] = print) -> dict:
    """Send `apk` to the phone in pieces, keep what arrived, join and check it there, and install it.
    Each piece gets `tries` tries, and the first one a run sends `first_extra` more. A piece that fails is sent again in
    smaller parts (down to `min_part` bytes), and once a push has run slowly the next pieces start in parts, so that
    each push stays under about half of `slow_s` seconds at the speed the last one had (see _parts_for).
    Answers {outcome: success | failure | refused, why, md5, size, pieces, sent, reused, failed_tries, read_again,
    split, parts_sent, waiting_for}, where read_again counts the pieces found whole only on the second read of their md5,
    split counts the pieces that were sent in parts and parts_sent those parts, and waiting_for names a dialog on the
    phone that holds the install, or is None."""
    if not os.path.isfile(apk):
        return {"outcome": "refused", "why": f"no file at {apk}"}
    run = run or _runner(adb, env)
    size, md5 = os.path.getsize(apk), md5_of(apk)
    here = f"{folder.rstrip('/')}/{md5}"
    q = shlex.quote(here)
    report = {"outcome": "failure", "why": "", "serial": serial, "apk": apk, "md5": md5, "size": size,
              "pieces": 0, "sent": 0, "reused": 0, "failed_tries": 0, "read_again": 0, "split": 0, "parts_sent": 0,
              "waiting_for": None}

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
        failed = _send_and_start(serial, apk, here, size, md5, piece, tries, first_extra, piece_s, slow_s, min_part,
                                 every, run, sleep, clock, say, report)
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


def _piece_md5(serial: str, run: Runner, here: str, name: str) -> str | None:
    """The md5 of the piece `name` in `here` on the phone, or None when it is not there or the phone did not say."""
    _code, got = _shell(serial, run, f"md5sum {shlex.quote(here + '/' + name)} 2>/dev/null")
    return md5_lines(got).get(name)


def _push_checked(serial: str, run: Runner, local: str, here: str, name: str, want: str, piece_s: float, every: float,
                  sleep, clock) -> dict:
    """Push the file `local` to `here/name`, and decide by the phone's md5 of it, not by adb's exit code (a push can arrive whole
    while adb says it failed, or while the md5 read gets no answer). When it is not there whole, the link is made again and the
    md5 read once more before the push counts as lost. Answers {arrived, code, out, got, again, back, secs}: `again` is whether
    the second read decided, `back` whether the link answered after a drop (None when no drop was seen), and `secs` how long
    the push itself ran."""
    start = clock()
    code, out = run(["-s", serial, "push", local, f"{here}/{name}"], timeout=piece_s)
    secs = clock() - start
    got = _piece_md5(serial, run, here, name)
    again, back = False, None
    if got != want:
        back = reconnect(serial, run)
        if not back:
            sleep(every)
        got, again = _piece_md5(serial, run, here, name), True
    return {"arrived": got == want, "code": code, "out": out, "got": got, "again": again, "back": back, "secs": secs}


def _send_parts(serial: str, run: Runner, here: str, p: dict, n: int, tmp: str, piece_s: float, every: float, sleep, clock,
                parts_there: dict[str, str]) -> dict:
    """One try at the piece `p` sent in `n` parts (see part_name), each checked by its md5 like a piece, then made into the piece
    on the phone and checked as a whole. A part already on the phone with the right md5 is not sent again, and one that arrived
    stays when a later one fails, so the next try sends only the rest. Answers what _push_checked does for the push that
    failed, with `part` (which one, from 1) and `n`, and `bytes` and `secs` for what this try pushed."""
    q = shlex.quote(here)
    idx = int(p["name"].split(".")[1])
    with open(p["path"], "rb") as f:
        blocks = cut(f.read(), n)
    n = len(blocks)
    res = {"arrived": False, "code": 0, "out": "", "got": None, "again": False, "back": None, "bytes": 0, "secs": 0.0,
           "part": None, "n": n}
    for k, data in enumerate(blocks):
        name = part_name(idx, n, k)
        want = hashlib.md5(data).hexdigest()   # noqa: S324 - see md5_of
        if parts_there.get(name) == want:
            continue
        local = os.path.join(tmp, name)
        fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        r = _push_checked(serial, run, local, here, name, want, piece_s, every, sleep, clock)
        res["again"] = res["again"] or r["again"]
        if not r["arrived"]:
            res.update(code=r["code"], out=r["out"], got=r["got"], back=r["back"], part=k + 1)
            return res
        parts_there[name] = want
        res["bytes"] += len(data)
        res["secs"] += r["secs"]
    tmpn = f"tmp.{idx:04d}"
    code, out = _shell(serial, run, f"cd {q} && cat part.{idx:04d}.of{n}.* > {tmpn} && mv {tmpn} {p['name']} && "
                                    f"rm -f part.{idx:04d}.* && echo merged", timeout=60)
    for name in [x for x in parts_there if x.startswith(f"part.{idx:04d}.")]:
        del parts_there[name]
    got = _piece_md5(serial, run, here, p["name"]) if "merged" in out else None
    if got == p["md5"]:
        res.update(arrived=True, got=got)
        return res
    _shell(serial, run, f"cd {q} && rm -f {tmpn} {p['name']} part.{idx:04d}.*")
    res.update(code=code, out="the parts joined on the phone did not make this piece, so they were removed"
               if "merged" in out else out, got=got, back=reconnect(serial, run), part=0)
    return res


def _send_and_start(serial: str, apk: str, here: str, size: int, md5: str, piece: int, tries: int, first_extra: int,
                    piece_s: float, slow_s: float, min_part: int, every: float, run: Runner, sleep, clock,
                    say: Callable[[str], None], report: dict) -> str | None:
    """Send what is missing of `apk` into `here` on the phone, join it there, check it, and start pm install.
    A piece goes whole while the link carries it, and in parts once it has failed or the last push that arrived
    ran slowly (see _parts_for). Counts into `report`, and answers why it failed, or None once the install has started."""
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
        _code, out = _shell(serial, run, f"cd {q} && md5sum part.* 2>/dev/null")
        parts_there = md5_lines(out)     # parts a run that failed left, which the next try of their piece does not send again
        say(f"{len(parts)} pieces of up to {piece // 1024} KiB for {os.path.basename(apk)} ({size} bytes); "
            f"{sum(1 for p in parts if there.get(p['name']) == p['md5'])} already on the phone")
        first = True
        rate = None            # bytes a second of the last push that arrived in this run
        for i, p in enumerate(parts, 1):
            if there.get(p["name"]) == p["md5"]:
                report["reused"] += 1
                continue
            n = tries + (first_extra if first else 0)
            first = False
            top = _max_parts(p["size"], min_part)
            nparts = _parts_for(p["size"], rate, slow_s, min_part)
            idx = int(p["name"].split(".")[1])
            left = [int(m.group(1)) for m in (re.match(rf"^part\.{idx:04d}\.of(\d+)\.\d+$", x) for x in parts_there) if m]
            if left:
                nparts = max(nparts, min(max(left), top))      # a run that failed left parts of this size: go on with them
            for t in range(1, n + 1):
                start = clock()
                if nparts == 1:
                    r = _push_checked(serial, run, p["path"], here, p["name"], p["md5"], piece_s, every, sleep, clock)
                    r.update(bytes=p["size"], part=None, n=1)
                else:
                    r = _send_parts(serial, run, here, p, nparts, tmp, piece_s, every, sleep, clock, parts_there)
                if r["arrived"]:
                    report["sent"] += 1
                    report["read_again"] += r["again"]
                    if nparts > 1:
                        report["split"] = report.get("split", 0) + 1
                        report["parts_sent"] = report.get("parts_sent", 0) + r["n"]
                    if r["bytes"] and r["secs"] >= 0.5:
                        rate = r["bytes"] / r["secs"]
                    code = r["code"]
                    note = (f"; its md5 matched on a second read (adb said exit {code})" if r["again"]
                            else f"; adb said exit {code}, and its md5 matched" if code != 0 and nparts == 1 else "")
                    how = f"sent in {r['n']} parts" if nparts > 1 else "sent"
                    say(f"piece {i} of {len(parts)} {how} ({p['size']} bytes, {clock() - start:.1f} s){note}")
                    break
                report["failed_tries"] += 1
                last = (r["out"].strip().splitlines() or [""])[-1][:200]
                got = r["got"]
                where = (f"try {t} of {n}, in {r['n']} parts, " + (f"part {r['part']} failed, " if r["part"] else "")
                         if nparts > 1 else f"try {t} of {n}, ")
                say(f"piece {i} of {len(parts)} did not arrive ({where}exit {r['code']}): {last}; on the phone: "
                    + (f"md5 {got[:12]}, not {p['md5'][:12]}" if got else "no md5"))
                if r["back"] is not None:      # what the next try can count on: how long this one ran and whether the link came back
                    say(f"  that try ran {clock() - start:.0f} s, and the link {'answered again' if r['back'] else 'did not answer again'}")
                if not (nparts > 1 and r["bytes"] > 0):
                    # a try that failed goes on in smaller parts, unless it moved a part: those stay, and the next try
                    # sends only the rest at the same size
                    nparts = min(max(nparts * 2, 2), top)
                if t < n:
                    sleep(every * t)      # a link that just dropped gets a little longer each time to come back
            else:
                return (f"piece {i} of {len(parts)} did not arrive in {n} tries; the "
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
    p.add_argument("--first-extra", type=int, default=FIRST_EXTRA,
                   help="more tries for the first piece a run sends")
    p.add_argument("--piece-s", type=float, default=PIECE_S, help="seconds one push may take")
    p.add_argument("--slow-s", type=float, default=SLOW_S,
                   help="a piece the last push's speed puts above this many seconds is sent in parts")
    p.add_argument("--min-part-kb", type=int, default=MIN_PART // 1024, help="the smallest part a piece is sent in")
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
                         tries=a.tries, first_extra=a.first_extra, piece_s=a.piece_s, slow_s=a.slow_s,
                         min_part=a.min_part_kb * 1024, install_s=a.install_s, adb=a.adb,
                         say=(lambda _s: None) if a.json else print)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(result["why"] if result["outcome"] == "success" else f"NOT INSTALLED: {result['why']}")
    return {"success": 0, "refused": 2}.get(result["outcome"], 1)
