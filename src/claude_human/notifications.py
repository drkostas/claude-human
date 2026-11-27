"""Notifications macOS has delivered and a person has not dismissed.

After a restart every app announces itself, and the banners pile up in Notification Center where
nothing else on the Mac counts them. The notification database keeps each delivered notification as a
16-byte id in the ``delivered`` table's list, grouped by the app that sent it, and the ``record`` table
holds the notification itself under that id. This reads the count, per app, without opening anything
for writing.

The database is found in two places on macOS 26, and only one of them is the real store. The group
container ``~/Library/Group Containers/group.com.apple.usernoted/db2/db`` is the one in use. The older
per-user folder (``$(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db``) still exists on
that version and is **zero bytes**. SQLite opens a zero-byte file without complaint and every query
then fails with "no such table", so a reader that trusted it would report no notifications for ever
and never an error. An empty file, a file without the tables, or no file at all is therefore refused
with ``StoreUnreadable``, which says which, and is never read as zero notifications.

The group container is protected, so the program that reads it needs the Full Disk Access grant. When
that is missing, ``StoreUnreadable`` carries the operating system's own message.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

#: the tables the count needs, as a database that is in use has them
TABLES = ("app", "delivered", "record")
#: one notification's id in a list is this many bytes
ID_BYTES = 16


class StoreUnreadable(Exception):
    """The notification store cannot be read, so nothing is known about the notifications."""


def candidates() -> list[Path]:
    """Where the store may be, the group container first."""
    found = [Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"]
    try:
        folder = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        folder = ""
    if folder:
        found.append(Path(folder) / "com.apple.notificationcenter" / "db2" / "db")
    return found


def _open(path: Path) -> sqlite3.Connection:
    """The store, opened read-only. A file that is not there, is empty, or lacks the tables is refused."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise StoreUnreadable(f"{path}: {e.strerror or e}") from e
    if size == 0:
        raise StoreUnreadable(f"{path} is empty (zero bytes), which is a stub and not the store")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as e:
        raise StoreUnreadable(f"{path}: {e}") from e
    missing = [t for t in TABLES if t not in have]
    if missing:
        conn.close()
        raise StoreUnreadable(f"{path} has no table {', '.join(missing)}, so it is not the notification store")
    return conn


def delivered(path: str | os.PathLike | None = None) -> dict:
    """``{"total": n, "apps": {bundle id: n}, "path": the store read}`` for the delivered notifications still
    in Notification Center. An app with none is left out. Raises StoreUnreadable when no store can be read.

    A notification is counted once, by the id in ``delivered`` that ``record`` also holds, so a list that
    names an id the store no longer has is not counted."""
    paths = [Path(path)] if path else candidates()
    errors = []
    for p in paths:
        try:
            conn = _open(p)
        except StoreUnreadable as e:
            errors.append(str(e))
            continue
        try:
            known = {bytes(u) for (u,) in conn.execute("SELECT uuid FROM record")}
            apps: dict[str, int] = {}
            rows = conn.execute("SELECT a.identifier, d.list FROM delivered d JOIN app a ON a.app_id = d.app_id "
                                "WHERE d.list IS NOT NULL").fetchall()
            for ident, blob in rows:
                blob = bytes(blob)
                ids = [blob[i:i + ID_BYTES] for i in range(0, len(blob) - ID_BYTES + 1, ID_BYTES)]
                n = sum(1 for i in ids if i in known)
                if n:
                    apps[ident] = apps.get(ident, 0) + n
        except sqlite3.Error as e:
            errors.append(f"{p}: {e}")
            continue
        finally:
            conn.close()
        return {"total": sum(apps.values()), "apps": apps, "path": str(p)}
    raise StoreUnreadable("; ".join(errors) or "no notification store found")
