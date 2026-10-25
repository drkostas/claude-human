"""Whether a Mac comes back to the desktop by itself after a restart."""
import plistlib
import subprocess

from claude_human import cli, unattended


def test_fdesetup_words_become_a_flag_and_anything_else_is_unknown():
    assert unattended.parse_fdesetup("FileVault is On.\n") is True
    assert unattended.parse_fdesetup("FileVault is Off.\nDecryption in progress: Percent completed = 40\n") is False
    assert unattended.parse_fdesetup("") is None and unattended.parse_fdesetup("fdesetup: not available") is None


def test_filevault_on_runs_fdesetup_and_is_unknown_when_it_does_not_answer():
    seen = []

    class R:
        returncode, stdout = 0, "FileVault is On.\n"

    assert unattended.filevault_on(run=lambda argv, **k: seen.append((argv, k.get("env"))) or R(),
                                   env={"PATH": "/usr/bin"}) is True
    assert seen == [([unattended.FDESETUP, "status"], {"PATH": "/usr/bin"})]

    class Failed:
        returncode, stdout = 1, ""

    assert unattended.filevault_on(run=lambda *a, **k: Failed()) is None

    def missing(*a, **k):
        raise FileNotFoundError(unattended.FDESETUP)

    assert unattended.filevault_on(run=missing) is None

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 20)

    assert unattended.filevault_on(run=slow) is None


def test_autologin_user_reads_the_login_window_settings(tmp_path):
    named = tmp_path / "named.plist"
    named.write_bytes(plistlib.dumps({"autoLoginUser": "alice", "SHOWFULLNAME": True}))
    unset = tmp_path / "unset.plist"
    unset.write_bytes(plistlib.dumps({"SHOWFULLNAME": True}))
    broken = tmp_path / "broken.plist"
    broken.write_bytes(b"not a plist")
    assert unattended.autologin_user(str(named)) == "alice"
    assert unattended.autologin_user(str(unset)) == ""
    assert unattended.autologin_user(str(broken)) is None
    assert unattended.autologin_user(str(tmp_path / "missing.plist")) is None


def test_the_verdict_needs_all_three_and_is_unknown_while_any_is():
    assert unattended.boots_unattended(False, "alice", True) is True
    assert unattended.boots_unattended(True, "alice", True) is False
    assert unattended.boots_unattended(False, "", True) is False
    assert unattended.boots_unattended(False, "alice", False) is False
    assert unattended.boots_unattended(None, "alice", True) is None
    assert unattended.boots_unattended(False, None, True) is None
    assert unattended.boots_unattended(False, "alice", None) is None


def test_the_words_say_each_part():
    assert unattended.words(True, "") == "FileVault on; no automatic login"
    assert unattended.words(False, "alice") == "FileVault off; automatic login for alice"
    assert unattended.words(False, "alice", False) == "FileVault off; automatic login for alice with no stored password"
    assert unattended.words(None, None) == "FileVault unknown; automatic login unknown"


def test_read_puts_it_together(tmp_path):
    plist = tmp_path / "loginwindow.plist"
    plist.write_bytes(plistlib.dumps({"autoLoginUser": "alice"}))
    stored = tmp_path / "kcpassword"
    stored.write_bytes(b"\x00")

    class Off:
        returncode, stdout = 0, "FileVault is Off.\n"

    got = unattended.read(run=lambda *a, **k: Off(), loginwindow=str(plist), kcpassword=str(stored))
    assert got == {"filevault_on": False, "autologin_user": "alice", "password_stored": True,
                   "unattended": True, "words": "FileVault off; automatic login for alice"}
    # the same Mac before the person set anything: on, nobody named, no stored password
    class On:
        returncode, stdout = 0, "FileVault is On.\n"

    plist.write_bytes(plistlib.dumps({}))
    got = unattended.read(run=lambda *a, **k: On(), loginwindow=str(plist), kcpassword=str(tmp_path / "none"))
    assert got["unattended"] is False and got["words"] == "FileVault on; no automatic login"


def test_cli_prints_the_reading_as_json(monkeypatch, capsys):
    monkeypatch.setattr(unattended, "read", lambda: {"filevault_on": True, "autologin_user": "",
                                                    "password_stored": False, "unattended": False,
                                                    "words": "FileVault on; no automatic login"})
    assert cli.main(["unattended"]) == 0
    out = capsys.readouterr().out
    assert '"unattended": false' in out and "FileVault on; no automatic login" in out
