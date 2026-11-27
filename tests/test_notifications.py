"""The notification count reads a store it can trust and refuses one it cannot."""
import sqlite3
import uuid

import pytest

from claude_human import notifications


def make_store(path, apps):
    """A store shaped like the real one. `apps` maps a bundle id to (delivered count, ids the record lacks)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER NULL);"
        "CREATE TABLE delivered (app_id INTEGER PRIMARY KEY, list BLOB);"
        "CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB);")
    for n, (ident, (count, ghosts)) in enumerate(apps.items(), start=1):
        conn.execute("INSERT INTO app(app_id, identifier) VALUES (?, ?)", (n, ident))
        ids = [uuid.uuid4().bytes for _ in range(count)]
        for u in ids:
            conn.execute("INSERT INTO record(app_id, uuid) VALUES (?, ?)", (n, u))
        ids += [uuid.uuid4().bytes for _ in range(ghosts)]
        conn.execute("INSERT INTO delivered(app_id, list) VALUES (?, ?)", (n, b"".join(ids) if ids else None))
    conn.commit()
    conn.close()


def test_counts_each_apps_delivered_notifications(tmp_path):
    p = tmp_path / "db"
    make_store(p, {"org.example.one": (3, 0), "org.example.two": (1, 0), "org.example.quiet": (0, 0)})
    got = notifications.delivered(p)
    assert got["total"] == 4
    assert got["apps"] == {"org.example.one": 3, "org.example.two": 1}


def test_an_id_the_store_no_longer_holds_is_not_counted(tmp_path):
    p = tmp_path / "db"
    make_store(p, {"org.example.one": (2, 5)})
    assert notifications.delivered(p)["total"] == 2


def test_a_zero_byte_file_is_refused_not_read_as_zero(tmp_path):
    p = tmp_path / "db"
    p.write_bytes(b"")
    with pytest.raises(notifications.StoreUnreadable, match="zero bytes"):
        notifications.delivered(p)


def test_a_database_without_the_tables_is_refused(tmp_path):
    p = tmp_path / "db"
    sqlite3.connect(p).execute("CREATE TABLE other (x)").connection.commit()
    with pytest.raises(notifications.StoreUnreadable, match="not the notification store"):
        notifications.delivered(p)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(notifications.StoreUnreadable):
        notifications.delivered(tmp_path / "absent")


def test_the_stub_first_falls_through_to_the_real_store(tmp_path, monkeypatch):
    stub, real = tmp_path / "stub", tmp_path / "real"
    stub.write_bytes(b"")
    make_store(real, {"org.example.one": (2, 0)})
    monkeypatch.setattr(notifications, "candidates", lambda: [stub, real])
    assert notifications.delivered()["total"] == 2


def test_only_stubs_is_an_error_naming_them(tmp_path, monkeypatch):
    stub = tmp_path / "stub"
    stub.write_bytes(b"")
    monkeypatch.setattr(notifications, "candidates", lambda: [stub])
    with pytest.raises(notifications.StoreUnreadable, match="stub"):
        notifications.delivered()


def test_the_store_is_never_opened_for_writing(tmp_path):
    p = tmp_path / "db"
    make_store(p, {"org.example.one": (1, 0)})
    before = p.read_bytes()
    notifications.delivered(p)
    assert p.read_bytes() == before
