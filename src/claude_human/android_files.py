"""New files in a folder on an Android phone, over adb: list them, wait for a burst to settle, pull
them, and lay them out on one contact sheet.

The usual case is "the person is about to take some photos; tell me when they are done, and show me
what they took". A camera writes a burst of files over a few seconds or minutes, so a file that is
there is not the end of it. A burst is settled once its newest file is `quiet` seconds old by the
phone's own clock, so one listing answers the question and no state has to be kept between them.

Everything here only reads the phone: `find` and `stat` in its shell, and `adb pull`. Nothing taps,
moves or deletes anything. Hidden files are left out (a camera writes `.pending-*` while it saves and
keeps `.trashed-*` after a delete), and so are folders (a burst folder is a folder of its own to ask
about).

    claude-human android files --serial HOST:PORT --since 2026-01-31T15:00:00+02:00 \\
        [--folder /sdcard/DCIM/Camera] [--quiet 90] [--wait 7200] [--pull DIR] [--sheet PNG] [--json]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence

ADB = "adb"
CAMERA = "/sdcard/DCIM/Camera"
#: what counts as a photo by default (the names are compared in lower case)
IMAGES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".dng")
#: seconds the newest file must be old before a burst is called settled
QUIET = 90.0

#: run(argv) -> (exit code, stdout as text); swapped in tests
Runner = Callable[[Sequence[str]], tuple[int, str]]


def _runner(adb: str, env: Mapping[str, str] | None) -> Runner:
    def run(argv: Sequence[str]) -> tuple[int, str]:
        try:
            r = subprocess.run([adb, *argv], capture_output=True, text=True, timeout=120, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return r.returncode, r.stdout + r.stderr
    return run


def since_time(text: str | float) -> float:
    """Seconds since the epoch, from an ISO 8601 time with its zone or a number of seconds.
    ValueError for anything else, a time without a zone included (whose clock it means is a guess)."""
    if isinstance(text, (int, float)):
        return float(text)
    try:
        return float(text)
    except ValueError:
        pass
    at = dt.datetime.fromisoformat(text.strip())
    if at.tzinfo is None:
        raise ValueError(f"a time needs its zone, like 2026-01-31T15:00:00+02:00, not {text!r}")
    return at.timestamp()


def listing(out: str, since: float, exts: Sequence[str] | None = IMAGES) -> tuple[float | None, list[dict]]:
    """Pure. (the phone's now, the files newer than `since`, oldest first) from LIST's output: its
    first line is `date +%s`, then one `<mtime> <size> <path>` line per file. `exts` None keeps any
    name."""
    lines = out.splitlines()
    now = float(lines[0]) if lines and lines[0].strip().isdigit() else None
    files = []
    for line in lines[1:]:
        parts = line.rstrip("\r").split(" ", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        mtime, size, path = int(parts[0]), int(parts[1]), parts[2]
        name = path.rsplit("/", 1)[-1]
        if name.startswith(".") or mtime <= since:
            continue
        if exts is not None and not name.lower().endswith(tuple(exts)):
            continue
        files.append({"name": name, "path": path, "size": size, "mtime": mtime})
    files.sort(key=lambda f: (f["mtime"], f["name"]))
    return now, files


def newer(serial: str, since: float, folder: str = CAMERA, *, exts: Sequence[str] | None = IMAGES,
          adb: str = ADB, env: Mapping[str, str] | None = None, run: Runner | None = None,
          clock=time.time) -> dict:
    """The files in `folder` on the device `serial` changed after `since` (seconds since the epoch).

    {"outcome": "success", "phone_now", "files": [{"name", "path", "size", "mtime"}]}, or
    {"outcome": "failure", "why"} when adb or the folder did not answer. Only the folder itself, not
    the folders in it. `find -mmin` narrows the listing on the phone, with two minutes to spare for
    a clock that differs from this one, and the exact cut is made here."""
    run = run or _runner(adb, env)
    minutes = max(1, math.ceil((clock() - since) / 60) + 2)
    command = (f"date +%s; find {shlex.quote(folder)} -maxdepth 1 -type f -mmin -{minutes} "
               f"-exec stat -c '%Y %s %n' {{}} +")
    code, out = run(["-s", serial, "shell", command])
    now, files = listing(out, since, exts)
    if code != 0 or now is None:
        said = " ".join(out.split())[:200] or f"exit code {code}"
        return {"outcome": "failure", "why": f"the phone did not list {folder}: {said}"}
    return {"outcome": "success", "phone_now": now, "files": files}


def settled(files: Sequence[dict], phone_now: float, quiet: float = QUIET) -> bool:
    """Pure. There are new files, and the newest is at least `quiet` seconds old on the phone's clock."""
    return bool(files) and phone_now - max(f["mtime"] for f in files) >= quiet


def wait_new(serial: str, since: float, folder: str = CAMERA, *, quiet: float = QUIET,
             wait: float = 7200.0, every: float = 20.0, exts: Sequence[str] | None = IMAGES,
             adb: str = ADB, env: Mapping[str, str] | None = None, run: Runner | None = None,
             sleep=time.sleep, clock=time.time) -> dict:
    """List `folder` every `every` seconds until a burst of files newer than `since` has settled.

    {"outcome": "success", "files", "why"}, {"outcome": "refused", "files", "why"} when `wait`
    seconds passed first (the files so far, perhaps none), or the failure of the last listing when
    the phone stopped answering at the end. `wait` 0 lists once."""
    end = clock() + wait
    while True:
        got = newer(serial, since, folder, exts=exts, adb=adb, env=env, run=run, clock=clock)
        if got["outcome"] == "success" and settled(got["files"], got["phone_now"], quiet):
            n = len(got["files"])
            return {"outcome": "success", "files": got["files"],
                    "why": f"{n} new file{'' if n == 1 else 's'} in {folder}, the newest "
                           f"{got['phone_now'] - got['files'][-1]['mtime']:.0f} s old"}
        if clock() >= end:
            if got["outcome"] != "success":
                return {**got, "files": []}
            n = len(got["files"])
            return {"outcome": "refused", "files": got["files"],
                    "why": (f"no new file in {folder} within {wait:g} s" if not n else
                            f"{n} new file{'' if n == 1 else 's'} in {folder}, but the newest is "
                            f"younger than {quiet:g} s")}
        sleep(every)


def pull(serial: str, files: Sequence[dict], dest: str, *, adb: str = ADB,
         env: Mapping[str, str] | None = None, run: Runner | None = None) -> list[str]:
    """Copy each file to the folder `dest` (made when missing) under its own name, and return the
    local paths of the ones that arrived whole. A file already there at the same size is not copied
    again, so a second call costs nothing."""
    run = run or _runner(adb, env)
    os.makedirs(dest, exist_ok=True)
    got = []
    for f in files:
        local = os.path.join(dest, f["name"])
        if not (os.path.exists(local) and os.path.getsize(local) == f["size"]):
            code, _ = run(["-s", serial, "pull", f["path"], local])
            if code != 0:
                continue
        if os.path.exists(local) and os.path.getsize(local) == f["size"]:
            got.append(local)
    return got


def contact_sheet(paths: Sequence[str], out: str, *, cell: int = 360, columns: int | None = None,
                  labels: Sequence[str] | None = None) -> str:
    """Lay the images out on one picture, `cell` pixels a side each, turned upright by their EXIF
    orientation, with a label under each (its file name, or `labels`). Returns `out`. Needs Pillow
    (`pip install claude-human[sheet]`); a file that is not an image gets a grey cell."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps   # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("the contact sheet needs Pillow: pip install 'claude-human[sheet]'") from e
    if not paths:
        raise ValueError("no images for a contact sheet")
    labels = list(labels) if labels is not None else [os.path.basename(p) for p in paths]
    cols = columns or min(len(paths), max(1, math.ceil(math.sqrt(len(paths)))))
    rows = math.ceil(len(paths) / cols)
    text_h = max(18, cell // 12)
    try:
        font = ImageFont.load_default(size=text_h - 4)
    except TypeError:   # Pillow before 10.1 has one size
        font = ImageFont.load_default()
    sheet = Image.new("RGB", (cols * cell, rows * (cell + text_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(paths):
        x, y = (i % cols) * cell, (i // cols) * (cell + text_h)
        try:
            with Image.open(path) as im:
                im.draft("RGB", (cell, cell))   # a large JPEG decodes at a fraction of its size
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((cell - 8, cell - 8))
                sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        except OSError:
            draw.rectangle((x + 4, y + 4, x + cell - 4, y + cell - 4), fill="#cccccc")
        draw.text((x + 6, y + cell + 1), labels[i], fill="black", font=font)
    sheet.save(out)
    return out


def main(argv: list[str]) -> int:
    """`claude-human android files ...` (android.main hands it the arguments after "files")."""
    import argparse   # noqa: PLC0415
    p = argparse.ArgumentParser(prog="claude-human android files",
                                description="new files in a phone folder, read only, over adb")
    p.add_argument("--serial", required=True, help="the device, as adb names it (HOST:PORT or a serial)")
    p.add_argument("--since", required=True,
                   help="files changed after this time: ISO 8601 with its zone, or seconds since the epoch")
    p.add_argument("--folder", default=CAMERA)
    p.add_argument("--all", action="store_true", help=f"any file, not only images ({' '.join(IMAGES)})")
    p.add_argument("--quiet", type=float, default=QUIET,
                   help="seconds the newest file must be old before the burst counts as settled")
    p.add_argument("--wait", type=float, default=0.0,
                   help="seconds to wait for a settled burst (0: list once and say whether it is settled)")
    p.add_argument("--every", type=float, default=20.0, help="seconds between listings while waiting")
    p.add_argument("--pull", metavar="DIR", help="copy the files into this folder")
    p.add_argument("--sheet", metavar="PNG", help="make one contact sheet of the pulled files (needs --pull)")
    p.add_argument("--json", action="store_true", help="print the whole result as JSON")
    p.add_argument("--adb", default=ADB)
    a = p.parse_args(argv)
    if a.sheet and not a.pull:
        p.error("--sheet needs --pull")
    try:
        since = since_time(a.since)
    except ValueError as e:
        p.error(str(e))
    result = wait_new(a.serial, since, a.folder, quiet=a.quiet, wait=a.wait, every=a.every,
                      exts=None if a.all else IMAGES, adb=a.adb)
    result.update(local=[], sheet=None)
    if a.pull and result["files"]:
        result["local"] = pull(a.serial, result["files"], a.pull, adb=a.adb)
        if a.sheet and result["local"]:
            result["sheet"] = contact_sheet(result["local"], a.sheet)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(result["why"] if result["outcome"] == "success" else f"NOT YET: {result['why']}")
        for f in result["local"] or [f["path"] for f in result["files"]]:
            print(f)
        if result["sheet"]:
            print(f"sheet: {result['sheet']}")
    return {"success": 0, "refused": 2}.get(result["outcome"], 1)
