"""A task store in one SQLite file, for a person with no database of their own.

The file is ``$CLAUDE_HUMAN_TASKS_DB``, or ``tasks.db`` in the data folder (``$CLAUDE_HUMAN_DATA_DIR``
or ``~/.local/share/claude-human``).

Whether a task is open is never stored. It is derived from the log of events: a task is open until
an event closes it (``task.done`` or ``task.withdraw``). Events are append only, and the file
refuses an UPDATE or a DELETE on them, so the history cannot be edited after the fact. A comment
is an event too, and it does not close anything.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from .model import Handoff, Task, _iso, check_answer

ENV_DB = "CLAUDE_HUMAN_TASKS_DB"
ENV_DATA_DIR = "CLAUDE_HUMAN_DATA_DIR"

CLOSING = ("task.done", "task.withdraw")

SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
  id         TEXT PRIMARY KEY,
  capability TEXT NOT NULL,
  subject    TEXT NOT NULL,
  reason     TEXT,
  steps      TEXT,
  verify     TEXT,
  owner      TEXT,
  actor      TEXT,
  tier       TEXT NOT NULL DEFAULT 'confirm',
  opened_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff (
  task_id    TEXT NOT NULL REFERENCES task(id),
  kind       TEXT NOT NULL,
  target     TEXT,
  label      TEXT,
  preference INTEGER NOT NULL DEFAULT 100,
  platform   TEXT NOT NULL DEFAULT 'any',
  prepare    TEXT
);
CREATE TABLE IF NOT EXISTS event (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  at       REAL NOT NULL,
  task_id  TEXT,
  action   TEXT NOT NULL,
  actor    TEXT,
  outcome  TEXT,
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS event_task ON event(task_id, action);
CREATE INDEX IF NOT EXISTS task_pair ON task(capability, subject);
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event
  BEGIN SELECT RAISE(ABORT, 'events are append only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event
  BEGIN SELECT RAISE(ABORT, 'events are append only'); END;
"""

_OPEN = ("NOT EXISTS (SELECT 1 FROM event e WHERE e.task_id = t.id AND e.action IN "
         "('task.done', 'task.withdraw'))")


def data_dir() -> Path:
    env = os.environ.get(ENV_DATA_DIR)
    return Path(env).expanduser() if env else Path.home() / ".local" / "share" / "claude-human"


def default_path() -> Path:
    env = os.environ.get(ENV_DB)
    return Path(env).expanduser() if env else data_dir() / "tasks.db"


def _handoff(h) -> Handoff:
    if isinstance(h, Handoff):
        return h
    if isinstance(h, dict):
        return Handoff(**h)
    raise TypeError(f"a handoff is a Handoff or a dict, not {type(h).__name__}")


class SqliteTaskStore:
    """The default ``TaskStore``. Each call opens its own connection, so one store can serve a
    threaded HTTP server."""

    def __init__(self, path: Optional[os.PathLike] = None):
        self.path = Path(path).expanduser() if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys = ON")
            yield c
        finally:
            c.close()

    @contextmanager
    def _write(self):
        """One write transaction, taken at once, so two writers cannot both see "no open task"."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _event(c, task_id, action, actor=None, outcome=None, detail=None) -> int:
        cur = c.execute("INSERT INTO event(at, task_id, action, actor, outcome, detail) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (time.time(), task_id, action, actor, outcome,
                         json.dumps(detail) if detail is not None else None))
        return cur.lastrowid

    @staticmethod
    def _find_open(c, capability, subject) -> Optional[str]:
        row = c.execute(f"SELECT t.id FROM task t WHERE t.capability = ? AND t.subject = ? "
                        f"AND {_OPEN} ORDER BY t.opened_at LIMIT 1", (capability, subject)).fetchone()
        return row[0] if row else None

    # ---- TaskStore ----
    def find_open(self, capability: str, subject: str) -> Optional[str]:
        with self._conn() as c:
            return self._find_open(c, capability, subject)

    def open_task(self, capability: str, subject: str, reason: Optional[str],
                  owner: Optional[str], *, actor: Optional[str] = None, steps: Optional[str] = None,
                  verify: Optional[list[str]] = None, handoffs: Iterable = (),
                  tier: str = "confirm") -> tuple[str, bool]:
        if verify is not None and (isinstance(verify, str) or not all(isinstance(a, str) for a in verify)):
            raise TypeError("verify is a list of arguments, never a shell string")
        hs = [_handoff(h) for h in handoffs]
        with self._write() as c:
            existing = self._find_open(c, capability, subject)
            if existing is not None:
                return existing, False
            task_id = str(uuid.uuid4())
            c.execute("INSERT INTO task(id, capability, subject, reason, steps, verify, owner, actor, "
                      "tier, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (task_id, capability, subject, reason, steps,
                       json.dumps(list(verify)) if verify is not None else None,
                       owner, actor, tier, time.time()))
            for h in hs:
                c.execute("INSERT INTO handoff(task_id, kind, target, label, preference, platform, "
                          "prepare) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (task_id, h.kind, h.target, h.label, h.preference, h.platform,
                           json.dumps(h.prepare) if h.prepare else None))
            self._event(c, task_id, "task.open", actor, "success", {"reason": reason})
        return task_id, True

    def _load(self, c, row) -> Task:
        (tid, cap, subj, reason, steps, verify, owner, actor, tier, opened) = row
        close = c.execute("SELECT at, action, outcome FROM event WHERE task_id = ? AND action IN "
                          "('task.done', 'task.withdraw') ORDER BY id LIMIT 1", (tid,)).fetchone()
        hs = [Handoff(k, t, l, p, pl, json.loads(pr) if pr else None)
              for k, t, l, p, pl, pr in c.execute(
                  "SELECT kind, target, label, preference, platform, prepare FROM handoff "
                  "WHERE task_id = ? ORDER BY rowid", (tid,))]
        state, closed_at, outcome = "open", None, None
        if close:
            closed_at, action, outcome = close
            state = "withdrawn" if action == "task.withdraw" else (
                "done" if outcome == "success" else "closed")
        return Task(id=tid, capability=cap, subject=subj, reason=reason, steps=steps,
                    verify=json.loads(verify) if verify else None, owner=owner, actor=actor,
                    tier=tier, state=state, opened_at=opened, closed_at=closed_at,
                    outcome=outcome, handoffs=hs)

    _COLS = "t.id, t.capability, t.subject, t.reason, t.steps, t.verify, t.owner, t.actor, t.tier, t.opened_at"

    def pending(self) -> list[Task]:
        """Open tasks, newest first: a task is raised when it needs doing, so the list agrees with
        the notification the person just read."""
        with self._conn() as c:
            rows = c.execute(f"SELECT {self._COLS} FROM task t WHERE {_OPEN} "
                             f"ORDER BY t.opened_at DESC, t.id DESC").fetchall()
            return [self._load(c, r) for r in rows]

    def get(self, task_id: str) -> Optional[Task]:
        with self._conn() as c:
            row = c.execute(f"SELECT {self._COLS} FROM task t WHERE t.id = ?", (str(task_id),)).fetchone()
            return self._load(c, row) if row else None

    def complete(self, task_id: str, outcome: str, evidence: dict) -> bool:
        """Close an open task with what was observed. ``unknown`` is recorded and leaves the task
        open, because nobody saw it done. Returns True only when this call closed the task."""
        check_answer(outcome, evidence)
        if outcome == "unknown":
            with self._write() as c:
                self._event(c, str(task_id), "task.unknown", None, "unknown", evidence)
            return False
        with self._write() as c:
            row = c.execute(f"SELECT t.id FROM task t WHERE t.id = ? AND {_OPEN}",
                            (str(task_id),)).fetchone()
            if row is None:
                return False
            self._event(c, str(task_id), "task.done", None, outcome, evidence)
            return True

    def withdraw(self, task_id: str, actor: Optional[str], reason: Optional[str]) -> dict:
        with self._write() as c:
            if c.execute("SELECT 1 FROM task WHERE id = ?", (str(task_id),)).fetchone() is None:
                return {"ok": False, "status": "refused", "outcome": "refused",
                        "reason": f"no such task: {task_id}"}
            if c.execute(f"SELECT 1 FROM task t WHERE t.id = ? AND {_OPEN}",
                         (str(task_id),)).fetchone() is None:
                return {"ok": False, "status": "refused", "outcome": "refused",
                        "reason": "the task is already closed"}
            eid = self._event(c, str(task_id), "task.withdraw", actor, "withdrawn",
                              {"reason": reason})
        return {"ok": True, "status": "done", "outcome": "success", "event": eid,
                "reason": reason}

    def comment(self, task_id: str, text: str, actor: Optional[str]) -> dict:
        said = (text or "").strip()
        if not said:
            return {"ok": False, "detail": "an empty comment says nothing, so nothing is recorded"}
        with self._write() as c:
            row = c.execute("SELECT subject FROM task WHERE id = ?", (str(task_id),)).fetchone()
            if row is None:
                return {"ok": False, "detail": f"no such task: {task_id}"}
            eid = self._event(c, str(task_id), "task.comment", actor, "success",
                              {"text": said[:2000]})
        return {"ok": True, "comment": eid, "subject": row[0], "intent": str(task_id)}

    def comments(self, task_id: Optional[str] = None) -> list[dict]:
        q = ("SELECT e.id, e.at, e.task_id, t.subject, e.actor, e.detail FROM event e "
             "LEFT JOIN task t ON t.id = e.task_id WHERE e.action = 'task.comment'")
        args: tuple = ()
        if task_id is not None:
            q += " AND e.task_id = ?"
            args = (str(task_id),)
        with self._conn() as c:
            return [{"id": i, "at": _iso(at), "intent": tid, "subject": s, "said_by": a,
                     "text": (json.loads(d) or {}).get("text") if d else None}
                    for i, at, tid, s, a, d in c.execute(q + " ORDER BY e.id", args)]

    def history(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT e.id, e.at, e.action, e.actor, e.outcome, e.detail, e.task_id, "
                "       t.capability, t.subject FROM event e LEFT JOIN task t ON t.id = e.task_id "
                "ORDER BY e.id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for eid, at, action, actor, outcome, detail, tid, cap, subj in rows:
            d = json.loads(detail) if detail else None
            says = None
            if isinstance(d, dict):
                says = d.get("text") or d.get("reason") or d.get("message")
            out.append({"id": eid, "at": _iso(at), "verb": action, "capability": cap,
                        "subject": subj if subj is not None else (d or {}).get("subject"),
                        "outcome": outcome, "origin": "observed" if action == "task.done" else None,
                        "actor": actor, "intent": tid, "says": says, "detail": d})
        return out

    def record_refusal(self, capability: str, subject: str, actor: Optional[str],
                       reason: str) -> None:
        with self._write() as c:
            self._event(c, None, "task.refused", actor, "refused",
                        {"capability": capability, "subject": subject, "reason": reason})

    def record(self, task_id: Optional[str], action: str, outcome: Optional[str] = None,
               detail: Optional[dict] = None) -> int:
        """Append any other event (``task.notify`` with the notifier's answer, for one)."""
        with self._write() as c:
            return self._event(c, task_id, action, None, outcome, detail)

