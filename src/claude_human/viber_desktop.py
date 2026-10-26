"""Read Viber Desktop's local message database, read only.

Viber Desktop keeps each signed-in account's messages in an SQLite file, `viber.db`, in a folder named
after the account under its data folder: `~/Library/Application Support/ViberPC/<account>/viber.db` on
macOS, `%APPDATA%\\ViberPC\\<account>\\viber.db` on Windows and `~/.ViberPC/<account>/viber.db` on Linux.
Reading that file shows nothing in the app and opens no chat. Opening a chat in the app's window is
what marks its messages seen for the sender, so a reader of the file can read a conversation without
telling anyone it was read.

- `databases(root)` lists the account files under a data folder (the platform's own by default).
- `snapshot(path)` copies a database, with what its write-ahead log holds, into memory through SQLite's
  backup, opening the file read only. The database and its log are never written. Like any SQLite
  reader beside a running writer, the connection takes a read mark in the log's shared-memory index
  (`viber.db-shm`) while it copies, which is how SQLite keeps the writer from overwriting pages a reader
  still needs. When the file cannot be opened read only, the three files are copied to a temporary
  folder and the copy is read. Everything else reads the snapshot.
- `missing(conn)` names each table and column this module reads that the file lacks. Viber's format is
  undocumented and changes between releases, so `chats` and `messages` raise `SchemaChanged` with that
  list rather than guess.
- `chats(conn)` lists the conversations, newest first, each with a title: the group's name, or the other
  person's name or number.
- `messages(conn, chat, limit)` gives the newest messages of one conversation, oldest first, as rows.
- `find(conn, title)` gives the conversations whose title is exactly `title`.

A row is {id, chat, from_me, sender, number, text, sent_at, kind, info}. `sent_at` is an ISO 8601 time
with its zone (UTC, or `tz` when given). `kind` names Viber's message type ("text", "image", "link",
"business", ...). `text` is the message's own text, or for a message whose text Viber keeps in its JSON
details (a business account's message, a link's preview, a file's caption) the first text found there.
`info` is those JSON details parsed, or None.

Nothing here sends a message or writes to the database.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

#: the file each account's messages are in
DB_NAME = "viber.db"

#: the tables and columns the readers use, by table (case does not matter to SQLite)
SCHEMA = {
    "ChatInfo": ("ChatID", "Name"),
    "ChatRelation": ("ChatID", "ContactID"),
    "Contact": ("ContactID", "Name", "ClientName", "Number"),
    "Events": ("EventID", "TimeStamp", "Direction", "ChatID", "ContactID", "SortOrder"),
    "Messages": ("EventID", "Type", "Body", "Info", "ClientFlag"),
}

#: Viber's message types, by the number in Messages.Type
KINDS = {1: "text", 2: "image", 3: "video", 4: "sticker", 5: "location", 8: "business", 9: "link",
         10: "contact", 11: "file", 15: "service", 72: "deleted"}
#: a reaction, kept as a message row of its own; not a message
REACTION = 0
#: Messages.ClientFlag values of the rows that keep an edited message's earlier text
EDIT_HISTORY = (256, 257)
#: Events.Direction of a message the account sent
OUTGOING = 1

#: keys, in order, whose value is taken as a message's text when its Body is empty
TEXT_KEYS = ("Text", "text", "Body", "body", "Message", "message", "Caption", "caption", "Title", "title",
             "Description", "description")

#: tries, and seconds between them, when the app holds the database locked for a moment
BUSY_TRIES, BUSY_WAIT_S = 5, 0.2


class SchemaChanged(RuntimeError):
    """The database lacks tables or columns the readers need (`missing`, a list of "Table.Column")."""

    def __init__(self, missing: list[str]):
        super().__init__("Viber's database lacks " + ", ".join(missing))
        self.missing = missing


def data_dir(home: str | os.PathLike | None = None, platform: str | None = None,
             environ: dict | None = None) -> Path:
    """The platform's Viber Desktop data folder."""
    home = Path(home) if home is not None else Path.home()
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        return home / "Library" / "Application Support" / "ViberPC"
    if platform.startswith("win"):
        return Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / "ViberPC"
    return home / ".ViberPC"


def databases(root: str | os.PathLike | None = None) -> list[Path]:
    """Each account's message database under `root` (the platform's data folder by default), the most
    recently written first. Empty when Viber Desktop was never signed in."""
    root = Path(root) if root is not None else data_dir()
    if not root.is_dir():
        return []
    found = [p for p in root.glob(f"*/{DB_NAME}") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _backup(src: sqlite3.Connection) -> sqlite3.Connection:
    mem = sqlite3.connect(":memory:")
    for attempt in range(BUSY_TRIES):
        try:
            src.backup(mem)
            return mem
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or attempt == BUSY_TRIES - 1:
                mem.close()
                raise
            time.sleep(BUSY_WAIT_S)
    raise AssertionError("unreachable")


def snapshot(path: str | os.PathLike) -> sqlite3.Connection:
    """An in-memory copy of the database at `path`, read through a read-only connection. The file and its
    write-ahead log are never written (the shared-memory index takes the reader's read mark)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        src = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        try:
            return _backup(src)
        finally:
            src.close()
    except sqlite3.OperationalError:
        pass
    # a log whose index this reader may not use: read a copy of all three files instead
    with tempfile.TemporaryDirectory(prefix="viber-") as tmp:
        for suffix in ("", "-wal", "-shm"):
            part = path.with_name(path.name + suffix)
            if part.is_file():
                shutil.copy2(part, Path(tmp) / (DB_NAME + suffix))
        src = sqlite3.connect(Path(tmp) / DB_NAME)
        try:
            return _backup(src)
        finally:
            src.close()


def missing(conn: sqlite3.Connection) -> list[str]:
    """Each "Table.Column" of SCHEMA the database lacks ("Table" alone for a missing table)."""
    out = []
    for table, cols in SCHEMA.items():
        have = {r[1].casefold() for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if not have:
            out.append(table)
            continue
        out += [f"{table}.{c}" for c in cols if c.casefold() not in have]
    return out


def _require(conn: sqlite3.Connection) -> None:
    gone = missing(conn)
    if gone:
        raise SchemaChanged(gone)


def _clean(value) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def self_contact(conn: sqlite3.Connection) -> int | None:
    """The account's own contact: the one most of its sent messages name, or None before it sent any."""
    row = conn.execute("SELECT ContactID FROM Events WHERE Direction = ? AND ContactID IS NOT NULL "
                       "GROUP BY ContactID ORDER BY COUNT(*) DESC LIMIT 1", (OUTGOING,)).fetchone()
    return row[0] if row else None


def _contacts(conn) -> dict[int, tuple[str | None, str | None]]:
    """{contact id: (name, number)}, the name as the address book has it, else as the person set it."""
    return {cid: (_clean(name) or _clean(client), _clean(number))
            for cid, name, client, number in conn.execute("SELECT ContactID, Name, ClientName, Number FROM Contact")}


def _members(conn) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for chat, contact in conn.execute("SELECT ChatID, ContactID FROM ChatRelation ORDER BY ContactID"):
        out.setdefault(chat, []).append(contact)
    return out


#: what counts as a message row (a reaction and an edit's earlier text do not)
_IS_MESSAGE = (f"m.Type <> {REACTION} AND COALESCE(m.ClientFlag, 0) NOT IN "
               f"({', '.join(str(f) for f in EDIT_HISTORY)})")


def chats(conn: sqlite3.Connection) -> list[dict]:
    """Every conversation that has a message, newest first: {chat, title, group, number, messages,
    last_at (milliseconds since the epoch)}. `number` is the other person's, for a conversation with one
    person."""
    _require(conn)
    me = self_contact(conn)
    people = _contacts(conn)
    members = _members(conn)
    out = []
    for chat, name, n, last in conn.execute(
            f"SELECT ci.ChatID, ci.Name, COUNT(e.EventID), MAX(e.TimeStamp) FROM ChatInfo ci "
            f"JOIN Events e ON e.ChatID = ci.ChatID JOIN Messages m ON m.EventID = e.EventID "
            f"WHERE {_IS_MESSAGE} GROUP BY ci.ChatID, ci.Name ORDER BY MAX(e.TimeStamp) DESC, ci.ChatID DESC"):
        others = [c for c in members.get(chat, []) if c != me]
        group = _clean(name) is not None or len(others) > 1
        person = people.get(others[0]) if others else None
        title = _clean(name) or (None if group else (person[0] or person[1]) if person else None)
        out.append({"chat": chat, "title": title, "group": group,
                    "number": None if group or not person else person[1], "messages": int(n), "last_at": last})
    return out


def find(conn: sqlite3.Connection, title: str) -> list[dict]:
    """The conversations whose title is exactly `title` (spaces collapsed), newest first."""
    want = _clean(title)
    return [c for c in chats(conn) if c["title"] == want] if want else []


def _info(raw) -> dict | list | None:
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def info_text(info, depth: int = 0) -> str | None:
    """Pure. The first text in a message's JSON details: the value of the first of TEXT_KEYS that holds a
    string, looked for at the top level first and then inside nested objects and lists."""
    if depth > 4 or info is None:
        return None
    if isinstance(info, dict):
        for key in TEXT_KEYS:
            if isinstance(info.get(key), str) and info[key].strip():
                return info[key].strip()
        nested = list(info.values())
    elif isinstance(info, list):
        nested = info
    else:
        return None
    for value in nested:
        if isinstance(value, (dict, list)):
            found = info_text(value, depth + 1)
            if found:
                return found
    return None


def _time(ms, tz: dt.tzinfo | None) -> str | None:
    try:
        when = dt.datetime.fromtimestamp(float(ms) / 1000, tz=dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return (when.astimezone(tz) if tz else when).isoformat()


def messages(conn: sqlite3.Connection, chat: int, limit: int = 50, *, tz: dt.tzinfo | None = None) -> list[dict]:
    """The newest `limit` messages of conversation `chat`, oldest first, as rows (see the module's
    docstring). The account's own messages have `from_me` True and no sender."""
    _require(conn)
    limit = max(1, int(limit))
    rows = conn.execute(
        f"SELECT * FROM (SELECT e.EventID, e.ChatID, e.TimeStamp, e.Direction, e.SortOrder, m.Type, m.Body, "
        f"m.Info, c.Name, c.ClientName, c.Number FROM Events e JOIN Messages m ON m.EventID = e.EventID "
        f"LEFT JOIN Contact c ON c.ContactID = e.ContactID WHERE e.ChatID = ? AND {_IS_MESSAGE} "
        f"ORDER BY e.TimeStamp DESC, e.SortOrder DESC, e.EventID DESC LIMIT ?) "
        f"ORDER BY TimeStamp, SortOrder, EventID", (chat, limit)).fetchall()
    out = []
    for eid, cid, ms, direction, _order, kind, body, raw, name, client, number in rows:
        mine = direction == OUTGOING
        info = _info(raw)
        text = (str(body).strip() if body is not None else "") or info_text(info)
        out.append({"id": eid, "chat": cid, "from_me": mine,
                    "sender": None if mine else (_clean(name) or _clean(client) or _clean(number)),
                    "number": None if mine else _clean(number), "text": text or None,
                    "sent_at": _time(ms, tz), "kind": KINDS.get(kind, f"type-{kind}"), "info": info})
    return out
