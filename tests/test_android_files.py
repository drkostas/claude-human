"""android_files lists the new files in a phone folder, says when a burst has settled, pulls them
and lays them out on a contact sheet, and only ever reads the phone."""
from __future__ import annotations

import json
import os

import pytest

from claude_human import android, android_files

SERIAL = "192.0.2.7:5555"
SINCE = 1_800_000_000          # the time the person was asked to take the photos


def stat_lines(now, files):
    """LIST's output: the phone's clock, then `<mtime> <size> <path>` per file."""
    return f"{now}\n" + "".join(f"{m} {s} /sdcard/DCIM/Camera/{n}\n" for n, m, s in files)


class Phone:
    """adb as android_files uses it: a shell listing answered from `files` and the phone's clock,
    and `pull` writing a file of the listed size. Every call is kept, to show nothing else is sent."""

    def __init__(self, now, files, *, fail=False):
        self.now, self.files, self.fail, self.calls = now, list(files), fail, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[2] == "shell":
            if self.fail:
                return 1, "adb: device offline\n"
            now = self.now() if callable(self.now) else self.now
            return 0, stat_lines(now, self.files)
        if argv[2] == "pull":
            name = argv[3].rsplit("/", 1)[-1]
            size = next(s for n, _m, s in self.files if n == name)
            with open(argv[4], "wb") as f:
                f.write(b"x" * size)
            return 0, f"{argv[3]}: 1 file pulled\n"
        return 1, "unexpected"


def test_since_time_needs_a_zone():
    assert android_files.since_time("2027-01-15T08:00:00+00:00") == 1_800_000_000
    assert android_files.since_time("1800000000") == 1_800_000_000
    with pytest.raises(ValueError, match="needs its zone"):
        android_files.since_time("2027-01-15T08:00:00")
    with pytest.raises(ValueError):
        android_files.since_time("yesterday")


def test_listing_keeps_new_photos_and_leaves_out_hidden_old_and_other_files():
    out = stat_lines(SINCE + 300, [
        ("IMG_0001.jpg", SINCE - 5, 10),            # before the time
        ("IMG_0002.jpg", SINCE + 20, 11),
        (".pending-1-IMG_0003.jpg", SINCE + 30, 0),  # still being saved
        (".trashed-1-IMG_0000.jpg", SINCE + 31, 9),  # deleted
        ("VID_0004.mp4", SINCE + 40, 99),
        ("IMG 0005.JPG", SINCE + 10, 12),            # a space in the name, upper case
    ]) + "garbage line\n"
    now, files = android_files.listing(out, SINCE)
    assert now == SINCE + 300
    assert [f["name"] for f in files] == ["IMG 0005.JPG", "IMG_0002.jpg"]
    assert files[0] == {"name": "IMG 0005.JPG", "path": "/sdcard/DCIM/Camera/IMG 0005.JPG",
                        "size": 12, "mtime": SINCE + 10}
    _, every = android_files.listing(out, SINCE, exts=None)
    assert [f["name"] for f in every] == ["IMG 0005.JPG", "IMG_0002.jpg", "VID_0004.mp4"]


def test_newer_lists_with_find_and_stat_only_and_quotes_the_folder():
    phone = Phone(SINCE + 600, [("IMG_1.jpg", SINCE + 60, 5)])
    got = android_files.newer(SERIAL, SINCE, "/sdcard/My Photos", run=phone, clock=lambda: SINCE + 600)
    assert got == {"outcome": "success", "phone_now": SINCE + 600,
                   "files": [{"name": "IMG_1.jpg", "path": "/sdcard/DCIM/Camera/IMG_1.jpg", "size": 5,
                              "mtime": SINCE + 60}]}
    [call] = phone.calls
    assert call[:3] == ["-s", SERIAL, "shell"]
    assert call[3] == ("date +%s; find '/sdcard/My Photos' -maxdepth 1 -type f -mmin -12 "
                       "-exec stat -c '%Y %s %n' {} +")


def test_a_phone_that_does_not_answer_is_a_failure_in_words():
    got = android_files.newer(SERIAL, SINCE, run=Phone(0, [], fail=True), clock=lambda: SINCE)
    assert got == {"outcome": "failure", "why": "the phone did not list /sdcard/DCIM/Camera: adb: device offline"}


def test_settled_needs_files_and_a_quiet_newest_one():
    files = [{"mtime": SINCE + 10}, {"mtime": SINCE + 50}]
    assert not android_files.settled([], SINCE + 999)
    assert not android_files.settled(files, SINCE + 100, quiet=90)
    assert android_files.settled(files, SINCE + 140, quiet=90)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_wait_new_waits_for_the_burst_to_settle_by_the_phones_clock():
    clock = Clock(SINCE + 30)
    phone = Phone(clock, [("IMG_1.jpg", SINCE + 40, 5), ("IMG_2.jpg", SINCE + 80, 6)])
    got = android_files.wait_new(SERIAL, SINCE, quiet=90, wait=600, every=20, run=phone,
                                 sleep=clock.sleep, clock=clock)
    assert got["outcome"] == "success"
    assert [f["name"] for f in got["files"]] == ["IMG_1.jpg", "IMG_2.jpg"]
    assert got["why"] == "2 new files in /sdcard/DCIM/Camera, the newest 90 s old"
    assert clock.t == SINCE + 80 + 90           # listed at 30, 50, ... 170 s


def test_wait_new_says_what_it_saw_when_time_runs_out():
    clock = Clock(SINCE)
    none = android_files.wait_new(SERIAL, SINCE, wait=60, every=20, run=Phone(clock, []),
                                  sleep=clock.sleep, clock=clock)
    assert none == {"outcome": "refused", "files": [], "why": "no new file in /sdcard/DCIM/Camera within 60 s"}
    clock = Clock(SINCE)
    young = android_files.wait_new(SERIAL, SINCE, wait=0, run=Phone(clock, [("IMG_1.jpg", SINCE + 1, 5)]),
                                   sleep=clock.sleep, clock=clock)
    assert young["outcome"] == "refused" and young["why"] == (
        "1 new file in /sdcard/DCIM/Camera, but the newest is younger than 90 s")


def test_pull_copies_each_file_once_and_never_writes_to_the_phone(tmp_path):
    phone = Phone(SINCE + 999, [("IMG_1.jpg", SINCE + 1, 5), ("IMG_2.jpg", SINCE + 2, 7)])
    files = android_files.newer(SERIAL, SINCE, run=phone, clock=lambda: SINCE + 999)["files"]
    dest = str(tmp_path / "photos")
    first = android_files.pull(SERIAL, files, dest, run=phone)
    assert first == [os.path.join(dest, "IMG_1.jpg"), os.path.join(dest, "IMG_2.jpg")]
    assert android_files.pull(SERIAL, files, dest, run=phone) == first
    pulls = [c for c in phone.calls if c[2] == "pull"]
    assert len(pulls) == 2                       # the second call found both already there
    # read only: a listing and pulls, never a tap, a move or a delete
    assert {c[2] for c in phone.calls} == {"shell", "pull"}
    shells = " ".join(c[3] for c in phone.calls if c[2] == "shell")
    for word in ("rm ", "mv ", "input ", "am ", "monkey"):
        assert word not in shells


def test_contact_sheet_lays_out_every_image_upright(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    paths = []
    for i, size in enumerate([(400, 300), (300, 400), (200, 200)]):
        p = tmp_path / f"IMG_{i}.jpg"
        Image.new("RGB", size, (200, 30 * i, 30)).save(p)
        paths.append(str(p))
    (tmp_path / "broken.jpg").write_bytes(b"not an image")
    paths.append(str(tmp_path / "broken.jpg"))
    out = android_files.contact_sheet(paths, str(tmp_path / "sheet.png"), cell=100)
    with Image.open(out) as sheet:
        assert sheet.size[0] == 200              # 2 columns for 4 pictures
        assert sheet.size[1] == 2 * (100 + 18)
        assert sheet.getpixel((50, 50))[0] > 150     # the first picture is there
    with pytest.raises(ValueError):
        android_files.contact_sheet([], str(tmp_path / "none.png"))


def test_cli_lists_pulls_and_makes_a_sheet(tmp_path, monkeypatch, capsys):
    Image = pytest.importorskip("PIL.Image")
    phone = Phone(SINCE + 999, [])

    def pull_a_real_jpeg(argv):
        phone.calls.append(list(argv))
        if argv[2] == "pull":
            Image.new("RGB", (40, 30), "red").save(argv[4])
            return 0, ""
        return 0, stat_lines(SINCE + 999, phone.files)

    monkeypatch.setattr(android_files, "_runner", lambda adb, env: pull_a_real_jpeg)
    # the phone lists the size of the file the fake pulls, so it arrives whole
    Image.new("RGB", (40, 30), "red").save(tmp_path / "probe.jpg")
    phone.files = [("IMG_1.jpg", SINCE + 1, os.path.getsize(tmp_path / "probe.jpg"))]
    dest, sheet = tmp_path / "got", tmp_path / "sheet.png"
    code = android.main(["files", "--serial", SERIAL, "--since", str(SINCE), "--pull", str(dest),
                         "--sheet", str(sheet), "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["local"] == [str(dest / "IMG_1.jpg")] and out["sheet"] == str(sheet)
    assert sheet.exists()


def test_cli_says_not_yet_and_refuses_a_time_without_a_zone(monkeypatch, capsys):
    monkeypatch.setattr(android_files, "_runner", lambda adb, env: Phone(SINCE + 10, []))
    assert android.main(["files", "--serial", SERIAL, "--since", str(SINCE)]) == 2
    assert capsys.readouterr().out.startswith("NOT YET: no new file in /sdcard/DCIM/Camera")
    with pytest.raises(SystemExit):
        android.main(["files", "--serial", SERIAL, "--since", "2027-01-15T08:00:00"])
