"""Viber Desktop's database read only, from an invented database with the app's tables."""
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from claude_human import viber_desktop as vd

SCHEMA_SQL = """
CREATE TABLE ChatInfo (ChatID integer primary key, Name text, Token text);
CREATE TABLE ChatRelation (ChatID integer, ContactID integer);
CREATE TABLE Contact (ContactID integer primary key, Name text, ClientName text, Number text);
CREATE TABLE Events (EventID integer primary key, TimeStamp integer, Direction integer, ChatID integer,
                     ContactID integer, SortOrder integer);
CREATE TABLE Messages (EventID integer primary key, Type integer, Body text, Info text, ClientFlag integer);
"""

#: 2026-01-02 10:00:00 UTC in milliseconds
T0 = 1767348000000


def _make(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.executescript(SCHEMA_SQL)
    conn.executemany("INSERT INTO Contact VALUES (?,?,?,?)", [
        (1, None, "Me", "+10000000001"),
        (2, "Alice Example", "alice", "+10000000002"),
        (3, None, "Courier Co", "+10000000003"),
        (4, None, None, "+10000000004"),
    ])
    conn.executemany("INSERT INTO ChatInfo VALUES (?,?,?)", [
        (10, None, "t1"), (20, None, "t2"), (30, "Trip planning", "t3"), (40, None, "t4")])
    conn.executemany("INSERT INTO ChatRelation VALUES (?,?)", [
        (10, 1), (10, 2), (20, 1), (20, 3), (30, 1), (30, 2), (30, 4), (40, 4), (40, 1)])
    ev = [
        # chat 10: Alice, two messages, one of the account's, a reaction and an edit's earlier text
        (101, T0, 0, 10, 2, 1, 1, "Hello there", None, None),
        (102, T0 + 60000, 1, 10, 1, 1, 1, "Hi Alice", None, None),
        (103, T0 + 61000, 0, 10, 2, 1, 0, "", None, None),
        (104, T0 + 62000, 0, 10, 2, 1, 1, "old text", None, 256),
        # chat 20: a business message whose text is only in its JSON details, and a link
        (201, T0 + 120000, 0, 20, 3, 1, 8, "",
         json.dumps({"Payload": {"Text": "Your parcel is in locker 7, code 123456, until Friday."}}), None),
        (202, T0 + 180000, 0, 20, 3, 1, 9, None, json.dumps({"Title": "Track it", "url": "https://example.org/t"}), None),
        # chat 30: a group
        (301, T0 + 240000, 0, 30, 4, 1, 1, "Who brings the map?", None, None),
        (302, T0 + 240000, 0, 30, 2, 2, 2, "", None, None),
        # chat 40: a person with no name
        (401, T0 + 30000, 0, 40, 4, 1, 1, "unknown sender", None, None),
    ]
    conn.executemany("INSERT INTO Events VALUES (?,?,?,?,?,?)", [e[:6] for e in ev])
    conn.executemany("INSERT INTO Messages VALUES (?,?,?,?,?)", [(e[0],) + e[6:] for e in ev])
    conn.commit()
    return conn


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "ViberPC" / "10000000001" / vd.DB_NAME
    path.parent.mkdir(parents=True)
    keep = _make(path)
    yield path
    keep.close()


def _digest(path: Path) -> dict:
    """The database and its log; the shared-memory index is where every reader takes its read mark."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.parent.iterdir())
            if p.name in (vd.DB_NAME, vd.DB_NAME + "-wal")}


def test_the_data_folder_follows_the_platform(tmp_path):
    assert vd.data_dir(tmp_path, "darwin") == tmp_path / "Library" / "Application Support" / "ViberPC"
    assert vd.data_dir(tmp_path, "linux") == tmp_path / ".ViberPC"
    assert vd.data_dir(tmp_path, "win32", {"APPDATA": str(tmp_path / "Roaming")}) == tmp_path / "Roaming" / "ViberPC"


def test_databases_lists_each_account_and_nothing_before_a_sign_in(tmp_path, db):
    assert vd.databases(tmp_path / "nowhere") == []
    (tmp_path / "ViberPC" / "config.db").write_bytes(b"")
    assert vd.databases(tmp_path / "ViberPC") == [db]


def test_a_snapshot_sees_what_is_still_in_the_log_and_writes_nothing(db):
    assert (db.parent / (vd.DB_NAME + "-wal")).stat().st_size > 0, "the rows are still in the log"
    before = _digest(db)
    snap = vd.snapshot(db)
    assert len(_digest(db)) == 2
    assert snap.execute("SELECT COUNT(*) FROM Events").fetchone()[0] == 9
    vd.chats(snap), vd.messages(snap, 10)
    assert _digest(db) == before, "the database and its log are left as they were"


def test_a_snapshot_reads_a_copy_when_the_file_cannot_be_opened_read_only(db, monkeypatch):
    real = sqlite3.connect

    def refuse_ro(target, *a, **kw):
        if isinstance(target, str) and target.endswith("?mode=ro"):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *a, **kw)
    monkeypatch.setattr(vd.sqlite3, "connect", refuse_ro)
    before = _digest(db)
    snap = vd.snapshot(db)
    assert snap.execute("SELECT COUNT(*) FROM Messages").fetchone()[0] == 9
    assert _digest(db) == before


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        vd.snapshot(tmp_path / "viber.db")


def test_a_changed_format_is_refused_by_name():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL.replace("ClientFlag integer", "Flags integer").replace(
        "CREATE TABLE ChatRelation (ChatID integer, ContactID integer);", ""))
    assert vd.missing(conn) == ["ChatRelation", "Messages.ClientFlag"]
    with pytest.raises(vd.SchemaChanged) as e:
        vd.chats(conn)
    assert e.value.missing == ["ChatRelation", "Messages.ClientFlag"]
    with pytest.raises(vd.SchemaChanged):
        vd.messages(conn, 1)


def test_chats_are_newest_first_with_a_title_for_each(db):
    snap = vd.snapshot(db)
    got = {c["chat"]: c for c in vd.chats(snap)}
    assert [c["chat"] for c in vd.chats(snap)] == [30, 20, 10, 40]
    assert got[10]["title"] == "Alice Example" and got[10]["number"] == "+10000000002" and not got[10]["group"]
    assert got[10]["messages"] == 2, "the reaction and the edit's earlier text are not messages"
    assert got[20]["title"] == "Courier Co", "a contact with no address-book name takes the name it set"
    assert got[30]["title"] == "Trip planning" and got[30]["group"] and got[30]["number"] is None
    assert got[40]["title"] == "+10000000004", "a person with no name at all is their number"
    assert vd.self_contact(snap) == 1


def test_find_matches_the_title_exactly(db):
    snap = vd.snapshot(db)
    assert [c["chat"] for c in vd.find(snap, "  Courier   Co ")] == [20]
    assert vd.find(snap, "Courier") == [] and vd.find(snap, "") == []


def test_messages_are_rows_oldest_first_and_the_account_has_no_sender(db):
    snap = vd.snapshot(db)
    rows = vd.messages(snap, 10)
    assert [r["id"] for r in rows] == [101, 102]
    assert rows[0] == {"id": 101, "chat": 10, "from_me": False, "sender": "Alice Example", "number": "+10000000002",
                       "text": "Hello there", "sent_at": "2026-01-02T10:00:00+00:00", "kind": "text", "info": None}
    assert rows[1]["from_me"] and rows[1]["sender"] is None and rows[1]["number"] is None
    assert vd.messages(snap, 10, tz=ZoneInfo("America/New_York"))[0]["sent_at"] == "2026-01-02T05:00:00-05:00"


def test_limit_keeps_the_newest(db):
    snap = vd.snapshot(db)
    assert [r["id"] for r in vd.messages(snap, 10, limit=1)] == [102]
    assert [r["id"] for r in vd.messages(snap, 30, limit=5)] == [301, 302], "the same time is ordered by SortOrder"


def test_a_text_kept_only_in_the_details_is_the_message_text(db):
    rows = vd.messages(vd.snapshot(db), 20)
    assert rows[0]["kind"] == "business"
    assert rows[0]["text"] == "Your parcel is in locker 7, code 123456, until Friday."
    assert rows[1]["kind"] == "link" and rows[1]["text"] == "Track it"
    assert rows[1]["info"]["url"] == "https://example.org/t"


def test_info_text_looks_at_the_top_first_then_inside():
    assert vd.info_text({"text": "top", "inner": {"Text": "deep"}}) == "top"
    assert vd.info_text({"a": [{"b": 1}, {"caption": " c "}]}) == "c"
    assert vd.info_text({"text": "   "}) is None
    assert vd.info_text(None) is None and vd.info_text("str") is None


def test_an_unknown_type_keeps_its_number(db):
    conn = vd.snapshot(db)
    at = T0 + 999000
    conn.execute("INSERT INTO Events VALUES (501, ?, 0, 10, 2, 1)", (at,))
    conn.execute("INSERT INTO Messages VALUES (501, 99, 'x', NULL, NULL)")
    row = vd.messages(conn, 10, limit=1)[0]
    assert row["kind"] == "type-99"
    assert dt.datetime.fromisoformat(row["sent_at"]).timestamp() * 1000 == at
