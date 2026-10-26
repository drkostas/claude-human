"""Pictures that go with a task or a question, for a person to look at on a phone.

A question about how something looks ("is the new layout right?") needs the picture beside it. A
path on the computer means nothing on the phone, so the picture is copied into a folder of its own,
named by its content, and handed back by that name as a ``data:`` URI that a page can show as it is.

    from claude_human.tasks import pictures

    p = pictures.store("/tmp/render/front.png")    # {"id", "name", "width", "height", "bytes"}
    uri = pictures.data_uri(p["id"])                # "data:image/png;base64,..." or None
    pictures.paths_in("The card is ready (picture at /tmp/render/front.png).")

- ``store`` refuses a path that is not a file, not a PNG, JPEG, GIF, WebP or HEIC picture (read
  from its first bytes, never its name), or larger than ``MAX_BYTES``. On macOS it shrinks a picture
  whose longer side is over ``max_side`` with ``sips``, and turns HEIC into JPEG, which browsers
  show. Elsewhere the file is copied as it is.
- The id is the SHA-256 of the original file, so the same picture stored twice is one file.
- ``data_uri`` takes only an id of that form, so a request can never name a path outside the folder.
- ``paths_in`` finds the absolute (or ``~/``) paths of existing pictures in a text, in order, once each.

The folder is ``CLAUDE_HUMAN_PICTURES``, else ``~/.local/share/claude-human/pictures``.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

ENV_FOLDER = "CLAUDE_HUMAN_PICTURES"
#: a picture larger than this is refused rather than copied
MAX_BYTES = 25 * 1024 * 1024
#: the longest side a stored picture keeps, in pixels
MAX_SIDE = 1600
_ID = re.compile(r"^[0-9a-f]{64}$")
_PATH = re.compile(r"(?<![\w/.~-])((?:~|/)[^\s'\"()<>\[\]`]*?\.(?:png|jpe?g|gif|webp|heic))(?![\w])",
                   re.IGNORECASE)
_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


class NotAPicture(ValueError):
    """The path is not a picture this module stores, and why."""


def folder(override: str | os.PathLike | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_FOLDER)
    return Path(env).expanduser() if env else Path.home() / ".local" / "share" / "claude-human" / "pictures"


def kind(head: bytes) -> str | None:
    """The picture's extension from its first bytes: .png, .jpg, .gif, .webp or .heic, else None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return ".heic"
    return None


def _sips() -> str | None:
    return shutil.which("sips") or ("/usr/bin/sips" if Path("/usr/bin/sips").exists() else None)


def _size(path: Path, sips: str | None) -> tuple[int | None, int | None]:
    if sips is None:
        return None, None
    try:
        out = subprocess.run([sips, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    w = re.search(r"pixelWidth:\s*(\d+)", out)
    h = re.search(r"pixelHeight:\s*(\d+)", out)
    return (int(w.group(1)) if w else None), (int(h.group(1)) if h else None)


def store(path: str | os.PathLike, *, into: str | os.PathLike | None = None, max_side: int = MAX_SIDE) -> dict:
    """Copy the picture at `path` into the folder (shrunk, see the module), and describe it:
    {"id", "name", "width", "height", "bytes"}. Raises NotAPicture with the reason."""
    src = Path(path).expanduser()
    if not src.is_file():
        raise NotAPicture(f"{src} is not a file")
    size = src.stat().st_size
    if size > MAX_BYTES:
        raise NotAPicture(f"{src} is {size} bytes, more than {MAX_BYTES}")
    data = src.read_bytes()
    ext = kind(data[:16])
    if ext is None:
        raise NotAPicture(f"{src} is not a PNG, JPEG, GIF, WebP or HEIC picture")
    pid = hashlib.sha256(data).hexdigest()
    out_dir = folder(into)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sips = _sips()
    w, h = _size(src, sips)
    shrink = bool(sips and (ext == ".heic" or (w and h and max(w, h) > max_side)))
    # sips writes HEIC and WebP badly or not at all, so those become JPEG and PNG (PNG keeps alpha)
    keep = {".heic": ".jpg", ".webp": ".png"}.get(ext, ext) if shrink else ext
    dest = out_dir / f"{pid}{keep}"
    if not dest.exists():
        tmp = out_dir / f".{pid}{keep}"
        if shrink:
            fmt = {".jpg": "jpeg", ".png": "png", ".gif": "gif"}[keep]
            args = [sips, "-Z", str(max_side), "-s", "format", fmt]
            r = subprocess.run(args + [str(src), "--out", str(tmp)], capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or not tmp.is_file():
                raise NotAPicture(f"sips could not read {src}: {(r.stderr or r.stdout).strip()[:200]}")
        elif ext == ".heic":
            raise NotAPicture(f"{src} is HEIC, and without sips it cannot be turned into a picture browsers show")
        else:
            tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    w, h = _size(dest, sips)
    return {"id": pid, "name": src.name, "width": w, "height": h, "bytes": dest.stat().st_size}


def path_of(pid: str, *, into: str | os.PathLike | None = None) -> Path | None:
    """The stored file for an id, or None for an id of another form or one not stored."""
    if not _ID.match(pid or ""):
        return None
    for ext in _TYPES:
        p = folder(into) / f"{pid}{ext}"
        if p.is_file():
            return p
    return None


def data_uri(pid: str, *, into: str | os.PathLike | None = None) -> str | None:
    """The stored picture as a data: URI, or None (see path_of)."""
    p = path_of(pid, into=into)
    if p is None:
        return None
    return f"data:{_TYPES[p.suffix]};base64," + base64.b64encode(p.read_bytes()).decode()


def _head_kind(p: Path) -> str | None:
    try:
        with p.open("rb") as f:
            return kind(f.read(16))
    except OSError:
        return None


def paths_in(text: str | None) -> list[str]:
    """The existing pictures a text names by absolute or ~/ path, in order, each once."""
    seen, out = set(), []
    for m in _PATH.finditer(text or ""):
        raw = m.group(1).rstrip(".,;:")
        p = Path(raw).expanduser()
        if raw not in seen and p.is_file() and _head_kind(p) is not None:
            seen.add(raw)
            out.append(raw)
    return out
