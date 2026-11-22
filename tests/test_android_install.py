"""android_install sends an APK in pieces it checks one by one, keeps what arrived for the next run, drops and
makes again a connection that went offline, and installs the joined file on the phone itself."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import pytest

from claude_human import android, android_install

SERIAL = "192.0.2.7:5555"
FOLDER = "/data/local/tmp/apk-install"
PKG = "com.example.app"
HOME = ("  mCurrentFocus=Window{1a2b3c u0 com.example.launcher/com.example.launcher.Home}\n"
        "  mFocusedApp=ActivityRecord{4d5e6f u0 com.example.launcher/.Home t2}\n")
#: what a phone that dozes behind its lock screen while Play Protect asks about an install says
LOCKED_PROTECT = ("  mCurrentFocus=Window{68defeb u0 NotificationShade}\n"
                  "  mFocusedApp=ActivityRecord{146710967 u0 com.android.vending/"
                  "com.google.android.finsky.protectdialogs.activity.PlayProtectDialogsActivity t4715}\n")


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()   # noqa: S324 - the same checksum the module and md5sum use


class Phone:
    """adb as android_install uses it, over a file system kept in a dict. `drop` lists the pushes (1 for the
    first) that fail with a protocol fault and leave the device offline until a disconnect and a connect.
    `whole_eof` lists pushes that write the whole file and still exit 1 with "failed to read copy response: EOF",
    and `unread` pushes that exit 0 while the next md5sum of their file gets no answer.
    `max_push` is the most bytes one push carries: a bigger one fails with "failed to read copy response: EOF", writes nothing
    and leaves the device offline until a disconnect and a connect, as a push over a link that cannot stay up long enough
    does. `bps` makes every push take len / bps seconds on the clock `tick` moves, and `bad_merge` makes the phone join the
    parts of a piece into something else. `answer` is what pm install writes once it has been polled `slow` times.
    Every call is kept."""

    def __init__(self, *, drop=(), whole_eof=(), unread=(), answer="Success", slow=1, installed=None, corrupt=False,
                 focus=HOME, max_push=None, bps=None, bad_merge=False):
        self.files: dict[str, bytes] = {}
        self.dirs: dict[str, int] = {}
        self.drop, self.answer, self.slow, self.installed, self.corrupt = set(drop), answer, slow, installed, corrupt
        self.whole_eof, self.unread, self.silent = set(whole_eof), set(unread), set()
        self.offline, self.pushes, self.polls, self.calls, self.focus = False, 0, 0, [], focus
        self.starts, self.reading = 0, {}             # pm installs started; {app.apk path: pid} while one runs
        self.max_push, self.bps, self.bad_merge, self.tick, self.pushed = max_push, bps, bad_merge, None, []

    def ls(self, folder):
        return sorted(p for p in self.files if p.rsplit("/", 1)[0] == folder)

    def __call__(self, argv, timeout=120.0):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "disconnect":
            self.offline = False
            return 0, f"disconnected {argv[1]}\n"
        if argv[0] == "connect":
            return 0, f"connected to {argv[1]}\n"
        assert argv[:2] == ["-s", SERIAL], argv
        if argv[2] == "get-state":
            return (1, "error: device offline\n") if self.offline else (0, "device\n")
        if self.offline:
            return 1, "adb: error: failed to get feature set: device offline\n"
        if argv[2] == "push":
            self.pushes += 1
            self.pushed.append(argv[4].rsplit("/", 1)[1])
            if self.pushes in self.drop:
                self.offline = True
                return 134, "adb F file_sync_client.cpp:477 protocol fault: failed to read stat response\n"
            data = open(argv[3], "rb").read()
            if self.tick and self.bps:
                self.tick(len(data) / self.bps)
            if self.max_push is not None and len(data) > self.max_push:
                self.offline = True
                return 1, "adb: error: failed to read copy response: EOF\n"
            if self.corrupt and argv[4].endswith("piece.0001"):
                # the last byte flipped, so the piece always differs (a fixed byte equals the file's own one in 256 runs)
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            self.files[argv[4]] = data
            if self.pushes in self.whole_eof:
                self.offline = True
                return 1, "adb: error: failed to read copy response: EOF\n"
            if self.pushes in self.unread:
                self.silent.add(argv[4])
            return 0, f"{argv[3]}: 1 file pushed, 0 skipped.\n"
        assert argv[2] == "shell", argv
        return self.shell(argv[3])

    def shell(self, cmd):
        m = re.match(r"^mkdir -p (\S+) && chmod 700 (\S+) (\S+) && for d in (\S+)/\*; do", cmd)
        if m:
            here, top = m.group(1), m.group(2)
            self.dirs[top] = self.dirs[here] = 0o700
            for p in [p for p in self.files if p.startswith(top + "/") and not p.startswith(here + "/")]:
                del self.files[p]
            return 0, "ready\n"
        m = re.match(r"^cd (\S+) && md5sum piece\.\* 2>/dev/null$", cmd)
        if m:
            return 0, "".join(f"{md5(self.files[p])}  {p.rsplit('/', 1)[1]}\n" for p in self.ls(m.group(1))
                              if "/piece." in p)
        m = re.match(r"^cd (\S+) && md5sum part\.\* 2>/dev/null$", cmd)
        if m:
            return 0, "".join(f"{md5(self.files[p])}  {p.rsplit('/', 1)[1]}\n" for p in self.ls(m.group(1))
                              if "/part." in p)
        m = re.match(r"^cd (\S+) && cat (part\.\d{4}\.of\d+\.)\* > (tmp\.\d{4}) && mv (tmp\.\d{4}) (piece\.\d{4}) && "
                     r"rm -f (part\.\d{4}\.)\* && echo merged$", cmd)
        if m:
            here = m.group(1)
            parts = [p for p in self.ls(here) if p.rsplit("/", 1)[1].startswith(m.group(2))]
            joined = b"".join(self.files[p] for p in parts)
            self.files[f"{here}/{m.group(5)}"] = joined[:-1] + b"?" if self.bad_merge and joined else joined
            for p in [p for p in self.ls(here) if p.rsplit("/", 1)[1].startswith(m.group(6))]:
                del self.files[p]
            return 0, "merged\n"
        m = re.match(r"^cd (\S+) && rm -f ([^&{;]+)$", cmd)
        if m:
            for pattern in m.group(2).split():
                for p in [p for p in self.ls(m.group(1)) if fnmatch.fnmatch(p.rsplit("/", 1)[1], pattern)]:
                    del self.files[p]
            return 0, ""
        m = re.match(r"^md5sum (\S+) 2>/dev/null$", cmd) or re.match(r"^md5sum (\S+)$", cmd)
        if m:
            p = m.group(1)
            if p == "/data/app/x/base.apk":
                return 0, f"{self.installed}  {p}\n"
            if p in self.silent:
                self.silent.discard(p)
                return 1, ""
            return (0, f"{md5(self.files[p])}  {p}\n") if p in self.files else (1, "")
        m = re.match(r"^cd (\S+) && cat piece\.\* > app\.apk && chmod 600 piece\.\* app\.apk && md5sum app\.apk$", cmd)
        if m:
            here = m.group(1)
            self.files[f"{here}/app.apk"] = b"".join(self.files[p] for p in self.ls(here) if "/piece." in p)
            self.joined_private = True
            return 0, f"{md5(self.files[here + '/app.apk'])}  app.apk\n"
        m = re.match(r"^rm -f (\S+)/\*$", cmd)
        if m:
            for p in self.ls(m.group(1)):
                del self.files[p]
            return 0, ""
        m = re.match(r"^cd (\S+) && rm -f install\.out && \{ setsid sh -c 'pm install -r -S (\d+) < app\.apk > "
                     r"install\.out 2>&1' < /dev/null > /dev/null 2>&1 & \} && echo started$", cmd)
        if m:
            assert int(m.group(2)) == len(self.files[m.group(1) + "/app.apk"])
            self.polls, self.starts = 0, self.starts + 1
            self.reading[m.group(1) + "/app.apk"] = 4242
            return 0, "started\n"
        m = re.match(r'^for p in /proc/\[0-9\]\*; do \[ "\$\(readlink \$p/fd/0 2>/dev/null\)" = (\S+) \] && '
                     r'echo "running \$\{p#/proc/\}"; done; true$', cmd)
        if m:
            pid = self.reading.get(m.group(1))
            return 0, f"running {pid}\n" if pid else ""
        if cmd == "dumpsys window | grep -E 'mFocusedApp|mCurrentFocus'":
            return 0, self.focus
        m = re.match(r"^cat (\S+)/install\.out 2>/dev/null$", cmd)
        if m:
            self.polls += 1
            if self.answer is not None and self.polls >= self.slow:
                self.reading.pop(m.group(1) + "/app.apk", None)
                if self.answer == "Success" and self.installed is None:
                    self.installed = md5(self.files[m.group(1) + "/app.apk"])
                return 0, self.answer + "\n"
            return 0, ""
        if cmd == f"pm path {PKG}":
            return 0, "package:/data/app/x/base.apk\n"
        m = re.match(r"^rm -rf (\S+)( && echo cleared)?$", cmd)
        if m:
            for p in [p for p in self.files if p.startswith(m.group(1) + "/")]:
                del self.files[p]
            return 0, "cleared\n" if m.group(2) else ""
        raise AssertionError(f"a shell command the fake phone does not know: {cmd}")


@pytest.fixture
def apk(tmp_path):
    p = tmp_path / "app.apk"
    p.write_bytes(os.urandom(10 * 1024 + 123))      # three pieces of 4 KiB and a short last one
    return str(p)


def run_install(phone, apk, **k):
    t = [0.0]
    phone.slept = []

    def sleep(s):
        t[0] += s
        phone.slept.append(s)
    phone.tick = lambda s: t.__setitem__(0, t[0] + s)
    said = []
    out = android_install.install(SERIAL, apk, run=phone, piece=4096, sleep=sleep, clock=lambda: t[0],
                                  say=said.append, **k)
    return out, said


def test_a_clean_install_sends_each_piece_installs_on_the_phone_and_removes_them(apk):
    phone = Phone()
    out, said = run_install(phone, apk, package=PKG)
    assert out["outcome"] == "success", out
    assert (out["pieces"], out["sent"], out["reused"], out["failed_tries"]) == (3, 3, 0, 0)
    assert "checked com.example.app by its md5" in out["why"]
    assert not phone.files, "the folder on the phone is removed after the install"
    assert not any(c[2:3] == ["install"] for c in phone.calls if len(c) > 2), "never one streamed adb install"
    assert phone.dirs[FOLDER] == 0o700 and phone.dirs[f"{FOLDER}/{out['md5']}"] == 0o700
    assert any("piece 3 of 3 sent" in s for s in said)


def test_a_dropped_piece_brings_the_device_back_and_is_sent_again(apk):
    phone = Phone(drop={2})
    out, said = run_install(phone, apk)
    assert out["outcome"] == "success" and out["failed_tries"] == 1 and out["sent"] == 3, out
    i = next(n for n, c in enumerate(phone.calls) if c[:1] == ["disconnect"])
    assert phone.calls[i + 1][:1] == ["connect"], "a connection adb holds as offline is dropped, then made again"
    assert any("piece 2 of 3 did not arrive (try 1 of 4, exit 134): adb F file_sync_client" in s
               and s.endswith("; on the phone: no md5") for s in said), said


def test_the_first_piece_a_run_sends_gets_more_tries_than_the_rest(apk):
    phone = Phone(drop={1, 2, 3, 4, 5})               # the first push fails five times and the sixth arrives
    out, said = run_install(phone, apk, tries=3)
    assert out["outcome"] == "success" and out["failed_tries"] == 5 and out["sent"] == 3, out
    assert any(s.startswith("piece 1 of 3 did not arrive (try 5 of 6, exit 134)") for s in said), said
    later = Phone(drop={2, 3, 4})                     # a piece after it keeps its own three
    out, _said = run_install(later, apk, tries=3)
    assert out["outcome"] == "failure" and "piece 2 of 3 did not arrive in 3 tries" in out["why"], out


def test_the_extra_tries_go_to_the_first_piece_sent_when_the_first_is_already_there(apk):
    phone = Phone(drop={1, 2, 3})
    here = f"{FOLDER}/{android_install.md5_of(apk)}"
    with open(apk, "rb") as f:
        phone.files[f"{here}/piece.0000"] = f.read(4096)
    out, said = run_install(phone, apk, tries=3)
    assert out["outcome"] == "success" and (out["reused"], out["sent"], out["failed_tries"]) == (1, 2, 3), out
    assert any(s.startswith("piece 2 of 3 did not arrive (try 3 of 6") for s in said), said


def test_each_further_try_waits_longer_than_the_one_before(apk):
    phone = Phone(drop={2, 3, 4})
    out, _said = run_install(phone, apk, tries=4, every=3.0)
    assert out["outcome"] == "success" and out["failed_tries"] == 3, out
    assert phone.slept[:3] == [3.0, 6.0, 9.0], phone.slept


def test_a_piece_that_arrived_whole_while_adb_said_eof_is_not_sent_again(apk):
    """adb can lose the link after the last byte of a push, while it waits for the phone's answer, and say
    "failed to read copy response: EOF" for a file that is whole on the phone. The md5 decides."""
    phone = Phone(whole_eof={1})
    out, said = run_install(phone, apk)
    assert out["outcome"] == "success" and (out["failed_tries"], out["read_again"]) == (0, 1), out
    assert phone.pushes == 3, "the piece that arrived is not pushed a second time"
    assert any(s.startswith("piece 1 of 3 sent") and s.endswith("its md5 matched on a second read (adb said exit 1)")
               for s in said), said


def test_a_push_that_exited_0_is_read_again_before_it_counts_as_lost(apk):
    phone = Phone(unread={1})                          # the read right after the push gets no answer
    out, said = run_install(phone, apk)
    assert out["outcome"] == "success" and (out["failed_tries"], out["read_again"]) == (0, 1), out
    assert phone.pushes == 3, "the piece that arrived is not pushed a second time"
    assert any(s.startswith("piece 1 of 3 sent") and s.endswith("(adb said exit 0)") for s in said), said


def test_what_arrived_stays_and_the_next_run_sends_only_the_rest(apk):
    phone = Phone(drop={3, 4, 5, 6})                  # piece 3 never arrives in this run
    out, _said = run_install(phone, apk)
    assert out["outcome"] == "failure" and "piece 3 of 3 did not arrive in 4 tries" in out["why"], out
    assert "the 2 pieces already there stay" in out["why"]
    assert len(phone.files) == 2
    phone.drop = set()
    again, said = run_install(phone, apk)
    assert again["outcome"] == "success" and (again["sent"], again["reused"]) == (1, 2), again
    assert "2 already on the phone" in said[0]


def test_another_apks_pieces_are_removed_before_a_new_one_starts(apk):
    phone = Phone()
    phone.files[f"{FOLDER}/{'0' * 32}/piece.0000"] = b"old"
    out, _said = run_install(phone, apk)
    assert out["outcome"] == "success" and not phone.files


def test_a_refused_install_says_the_phones_words_and_keeps_the_file(apk):
    phone = Phone(answer="Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]")
    out, _said = run_install(phone, apk)
    assert out["outcome"] == "failure" and out["why"].startswith("the phone refused the install: Failure ["), out
    assert phone.files, "the pieces stay for the next run"


def test_an_install_that_never_answers_ends_at_its_limit(apk):
    phone = Phone(answer=None)
    out, _said = run_install(phone, apk, install_s=30)
    assert out["outcome"] == "failure" and "did not answer within 30 s" in out["why"], out


def test_a_slow_install_is_waited_for(apk):
    phone = Phone(slow=5)
    out, _said = run_install(phone, apk, install_s=60)
    assert out["outcome"] == "success" and phone.polls == 5, out


def test_an_installed_copy_that_is_not_this_file_is_a_failure(apk):
    phone = Phone(installed="f" * 32)
    out, _said = run_install(phone, apk, package=PKG)
    assert out["outcome"] == "failure" and "is not this file" in out["why"], out


def test_a_changed_piece_fails_its_own_check_and_is_sent_again(apk):
    phone = Phone(corrupt=True)
    out, said = run_install(phone, apk, tries=1)
    assert out["outcome"] == "failure" and "piece 2 of 3 did not arrive in 1 tries" in out["why"], out
    with open(apk, "rb") as f:
        f.seek(4096)
        want = md5(f.read(4096))
    assert any(s.startswith("piece 2 of 3 did not arrive (try 1 of 1, exit 0)") and f", not {want[:12]}" in s
               and "on the phone: md5 " in s for s in said), said


def test_a_stray_piece_spoils_the_join_so_the_pieces_go_and_the_next_run_sends_them_again(apk):
    phone = Phone()
    here = f"{FOLDER}/{android_install.md5_of(apk)}"
    phone.dirs[here] = 0o700
    phone.files[f"{here}/piece.0009"] = b"left by a run with another piece size"
    out, _said = run_install(phone, apk)
    assert out["outcome"] == "failure" and "do not make this file (md5 differs)" in out["why"], out
    assert not phone.files
    again, _said = run_install(phone, apk)
    assert again["outcome"] == "success" and again["sent"] == 3, again


def test_a_device_that_never_answers_is_a_failure_before_anything_is_sent(apk):
    calls = []

    def never(argv, timeout=120.0):
        calls.append(list(argv))
        return (0, "") if argv[0] in ("disconnect", "connect") else (1, "error: device offline\n")
    out = android_install.install(SERIAL, apk, run=never, piece=4096, sleep=lambda s: None, say=lambda s: None)
    assert out["outcome"] == "failure" and out["why"] == "the device does not answer over adb", out
    assert not any("push" in c for c in calls)


def test_clear_removes_every_piece(apk):
    phone = Phone(drop={3, 4, 5, 6})
    run_install(phone, apk)
    assert phone.files
    out = android_install.clear(SERIAL, FOLDER, run=phone)
    assert out["outcome"] == "success" and not phone.files, out


def test_a_missing_apk_is_refused(tmp_path):
    out = android_install.install(SERIAL, str(tmp_path / "none.apk"), run=Phone())
    assert out["outcome"] == "refused"


def test_the_cli_answers_json_and_its_exit_codes(apk, capsys, monkeypatch):
    phone = Phone()
    monkeypatch.setattr(android_install, "_runner", lambda adb, env: phone)
    monkeypatch.setattr(android_install, "EVERY", 0.0)
    monkeypatch.setattr(android_install.time, "sleep", lambda s: None)
    code = android.main(["install", "--serial", SERIAL, apk, "--piece-kb", "4", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["outcome"] == "success" and result["pieces"] == 3, result
    assert android.main(["install", "--serial", SERIAL, apk + ".gone"]) == 2
    assert "NOT INSTALLED: no file at" in capsys.readouterr().out


def test_the_cli_passes_the_first_pieces_extra_tries(apk, capsys, monkeypatch):
    phone = Phone(drop={1})
    monkeypatch.setattr(android_install, "_runner", lambda adb, env: phone)
    monkeypatch.setattr(android_install.time, "sleep", lambda s: None)
    code = android.main(["install", "--serial", SERIAL, apk, "--piece-kb", "4", "--tries", "1", "--first-extra", "0"])
    assert code == 1 and "piece 1 of 3 did not arrive in 1 tries" in capsys.readouterr().out


def test_a_piece_the_link_cannot_carry_whole_is_sent_in_smaller_parts_and_arrives(apk):
    """A push that has to stay up too long is the one that fails on a slow link. The same piece sent in parts that are half as
    big, and again half as big, goes through, and the pieces are made on the phone, so the file is the same one."""
    phone = Phone(max_push=1500)
    out, said = run_install(phone, apk, min_part=512, package=PKG)
    assert out["outcome"] == "success", out
    # piece 1 and 2 fail whole and in halves, and arrive in quarters; piece 3 (2171 bytes) fails whole and arrives in halves
    assert out["failed_tries"] == 5 and out["split"] == 3 and out["parts_sent"] == 10, out
    assert any(s.startswith("piece 1 of 3 sent in 4 parts (4096 bytes") for s in said), said
    assert any(s.startswith("piece 3 of 3 sent in 2 parts (2171 bytes") for s in said), said


def test_parts_that_arrived_stay_when_a_later_part_fails_and_the_next_run_sends_only_the_rest(apk):
    """A try that failed but moved a part keeps its size, and the parts there are not sent again, in the next try and in the
    next run."""
    phone = Phone(max_push=2048, drop={3, 4})
    out, said = run_install(phone, apk, min_part=512, tries=3, first_extra=0)
    assert out["outcome"] == "failure" and "piece 1 of 3 did not arrive in 3 tries" in out["why"], out
    assert phone.pushed == ["piece.0000", "part.0000.of2.00", "part.0000.of2.01", "part.0000.of2.01"], phone.pushed
    phone.max_push, phone.drop, phone.pushed = None, set(), []
    out, said = run_install(phone, apk, min_part=512, tries=3, first_extra=0)
    assert out["outcome"] == "success", out
    assert phone.pushed == ["part.0000.of2.01", "piece.0001", "piece.0002"], phone.pushed


def test_a_slow_push_makes_the_next_pieces_start_in_parts_so_no_try_is_spent_on_a_whole_one(apk):
    phone = Phone(bps=200)       # 4096 bytes take 20.5 s
    out, said = run_install(phone, apk, slow_s=10.0, min_part=512, piece_s=1000.0)
    assert out["outcome"] == "success" and out["failed_tries"] == 0, out
    assert out["split"] == 2 and out["parts_sent"] == 12, out         # 8 parts of 512 bytes, then 4 parts of the short piece
    assert phone.pushed[0] == "piece.0000" and all(n.startswith("part.") for n in phone.pushed[1:]), phone.pushed


def test_a_fast_link_keeps_whole_pieces(apk):
    phone = Phone(bps=1_000_000)
    out, said = run_install(phone, apk, slow_s=10.0, min_part=512)
    assert out["outcome"] == "success" and out["split"] == 0 and out["parts_sent"] == 0, out
    assert phone.pushed == ["piece.0000", "piece.0001", "piece.0002"], phone.pushed


def test_a_failed_try_says_how_long_it_ran_and_whether_the_link_answered_again(apk):
    phone = Phone(max_push=1500, bps=100)
    out, said = run_install(phone, apk, min_part=512)
    assert out["outcome"] == "success", out
    assert any(re.match(r"^  that try ran 41 s, and the link answered again$", s) for s in said), said


def test_parts_that_do_not_make_the_piece_are_removed_and_the_try_is_lost(apk):
    phone = Phone(max_push=1500, bad_merge=True)
    out, said = run_install(phone, apk, min_part=512, tries=3, first_extra=0)
    assert out["outcome"] == "failure" and "piece 1 of 3 did not arrive in 3 tries" in out["why"], out
    assert any("the parts joined on the phone did not make this piece" in s for s in said), said
    assert not [p for p in phone.files if "/part." in p or "/tmp." in p or p.endswith("/piece.0000")], sorted(phone.files)


def test_a_piece_smaller_than_two_parts_is_never_split(apk):
    phone = Phone(max_push=1500)
    out, said = run_install(phone, apk, tries=2, first_extra=0)       # min_part is 256 KiB: a 4 KiB piece has no parts
    assert out["outcome"] == "failure" and out["split"] == 0 and out["parts_sent"] == 0, out
    assert phone.pushed == ["piece.0000", "piece.0000"], phone.pushed


def test_the_cli_passes_the_slow_and_the_part_limits(apk, monkeypatch):
    seen = {}
    monkeypatch.setattr(android_install, "install",
                        lambda serial, path, **k: seen.update(k) or {"outcome": "success", "why": "installed"})
    assert android_install.main(["--serial", SERIAL, apk, "--min-part-kb", "1", "--slow-s", "5"]) == 0
    assert seen["slow_s"] == 5.0 and seen["min_part"] == 1024, seen
    seen.clear()
    assert android_install.main(["--serial", SERIAL, apk]) == 0
    assert seen["slow_s"] == android_install.SLOW_S and seen["min_part"] == android_install.MIN_PART, seen


def test_the_start_answers_at_once_while_the_install_still_runs(tmp_path):
    """adb answers a shell command only once nothing holds its output open. A start line whose background part
    kept it open answered only when the install ended, so an install that waited longer than the call's limit
    read as "could not start", and the next run started a second install beside the first."""
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "pm").write_text("#!/bin/sh\ncat > /dev/null\nsleep 4\necho Success\n")
    (tools / "setsid").write_text('#!/bin/sh\nexec "$@"\n')
    for t in tools.iterdir():
        t.chmod(0o755)
    here = tmp_path / "here"
    here.mkdir()
    (here / "app.apk").write_bytes(b"0123456789")
    start = time.monotonic()
    r = subprocess.run(["/bin/sh", "-c", android_install.start_command(str(here), 10)], capture_output=True,
                       text=True, timeout=30, env={"PATH": f"{tools}:/usr/bin:/bin"})
    took = time.monotonic() - start
    assert r.stdout.strip() == "started" and took < 2.5, (took, r.stdout, r.stderr)
    out = here / "install.out"
    for _ in range(100):                               # the install itself goes on and answers in its file
        if out.exists() and "Success" in out.read_text():
            break
        time.sleep(0.1)
    assert "Success" in out.read_text()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd") or sys.platform == "darwin", reason="needs /proc")
def test_the_running_check_finds_a_process_that_reads_the_file(tmp_path):
    here = tmp_path / "here"
    here.mkdir()
    (here / "app.apk").write_bytes(b"0123456789")
    with open(here / "app.apk", "rb") as fh:
        reader = subprocess.Popen(["sleep", "30"], stdin=fh)
    try:
        r = subprocess.run(["/bin/sh", "-c", android_install.running_command(str(here))], capture_output=True,
                           text=True, timeout=30)
        assert f"running {reader.pid}" in r.stdout.splitlines(), r.stdout
    finally:
        reader.kill()
        reader.wait()
    r = subprocess.run(["/bin/sh", "-c", android_install.running_command(str(here))], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0 and "running" not in r.stdout


def test_a_run_that_finds_the_install_still_running_waits_for_it_and_starts_no_other(apk):
    phone = Phone(answer=None)
    first, _said = run_install(phone, apk, install_s=30)
    assert first["outcome"] == "failure" and phone.starts == 1, first
    pushes = phone.pushes
    phone.answer = "Success"                           # someone answered on the phone in the meantime
    again, said = run_install(phone, apk, package=PKG)
    assert again["outcome"] == "success", again
    assert phone.starts == 1 and phone.pushes == pushes, "nothing sent and no second install started"
    assert any("still running on the phone (pid 4242); waiting for it instead of starting another" in s
               for s in said), said


def test_a_play_protect_dialog_is_named_while_the_lock_screen_has_the_focus(apk):
    phone = Phone(answer=None, focus=LOCKED_PROTECT)
    out, said = run_install(phone, apk, install_s=90, look_s=30)
    assert out["outcome"] == "failure" and out["waiting_for"] == "a Google Play Protect dialog", out
    assert out["why"].startswith("the install waits for someone to answer a Google Play Protect dialog on the "
                                 "phone"), out
    assert said.count("the install waits for someone to answer a Google Play Protect dialog on the phone") == 1
    assert phone.files, "the file stays for the next run"


def test_an_install_with_no_dialog_still_reads_as_one_that_did_not_answer(apk):
    out, _said = run_install(Phone(answer=None), apk, install_s=30)
    assert out["waiting_for"] is None and "did not answer within 30 s" in out["why"], out


def test_only_the_focus_lines_name_a_dialog():
    assert android_install.prompt_shown(LOCKED_PROTECT) == "a Google Play Protect dialog"
    assert android_install.prompt_shown(HOME) is None
    assert android_install.prompt_shown("  mLastFocus=...protectdialogs.activity.PlayProtectDialogsActivity\n") is None


def test_a_phone_that_already_runs_this_file_gets_nothing_sent(apk):
    phone = Phone(installed=android_install.md5_of(apk))
    out, _said = run_install(phone, apk, package=PKG)
    assert out["outcome"] == "success" and "already runs this file" in out["why"], out
    assert phone.pushes == 0 and phone.starts == 0
