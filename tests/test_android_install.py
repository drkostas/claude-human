"""android_install sends an APK in pieces it checks one by one, keeps what arrived for the next run, drops and
makes again a connection that went offline, and installs the joined file on the phone itself."""
from __future__ import annotations

import hashlib
import json
import os
import re

import pytest

from claude_human import android, android_install

SERIAL = "192.0.2.7:5555"
FOLDER = "/data/local/tmp/apk-install"
PKG = "com.example.app"


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()   # noqa: S324 - the same checksum the module and md5sum use


class Phone:
    """adb as android_install uses it, over a file system kept in a dict. `drop` lists the pushes (1 for the
    first) that fail with a protocol fault and leave the device offline until a disconnect and a connect.
    `answer` is what pm install writes once it has been polled `slow` times. Every call is kept."""

    def __init__(self, *, drop=(), answer="Success", slow=1, installed=None, corrupt=False):
        self.files: dict[str, bytes] = {}
        self.dirs: dict[str, int] = {}
        self.drop, self.answer, self.slow, self.installed, self.corrupt = set(drop), answer, slow, installed, corrupt
        self.offline, self.pushes, self.polls, self.calls = False, 0, 0, []

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
            if self.pushes in self.drop:
                self.offline = True
                return 134, "adb F file_sync_client.cpp:477 protocol fault: failed to read stat response\n"
            data = open(argv[3], "rb").read()
            if self.corrupt and argv[4].endswith("piece.0001"):
                data = data[:-1] + b"!"
            self.files[argv[4]] = data
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
        m = re.match(r"^md5sum (\S+) 2>/dev/null$", cmd) or re.match(r"^md5sum (\S+)$", cmd)
        if m:
            p = m.group(1)
            if p == "/data/app/x/base.apk":
                return 0, f"{self.installed}  {p}\n"
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
        m = re.match(r"^cd (\S+) && rm -f install\.out && setsid sh -c 'pm install -r -S (\d+) < app\.apk > "
                     r"install\.out 2>&1' < /dev/null > /dev/null 2>&1 & echo started$", cmd)
        if m:
            assert int(m.group(2)) == len(self.files[m.group(1) + "/app.apk"])
            self.polls = 0
            return 0, "started\n"
        m = re.match(r"^cat (\S+)/install\.out 2>/dev/null$", cmd)
        if m:
            self.polls += 1
            if self.answer is not None and self.polls >= self.slow:
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

    def sleep(s):
        t[0] += s
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
    assert any("piece 2 of 3 did not arrive (try 1 of 4, exit 134): adb F file_sync_client" in s for s in said)


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
    out, _said = run_install(phone, apk, tries=1)
    assert out["outcome"] == "failure" and "piece 2 of 3 did not arrive in 1 tries" in out["why"], out


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
