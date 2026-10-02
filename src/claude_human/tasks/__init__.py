"""Tasks for a person: ask once, tell them, close only when an observation says it is done.

When an assistant needs a person to do something it cannot (approve a permission, sign in, plug a
cable in), it opens a task here.

    from claude_human import notify, tasks

    engine = tasks.TaskEngine(tasks.SqliteTaskStore(), notifier=notify.from_env(),
                              link="myapp://task/{id}")
    task_id, new = engine.request(
        "approve", "app://backup", "The backup needs Full Disk Access.", owner="backup-bot",
        steps="System Settings > Privacy & Security > Full Disk Access > enable Backup",
        verify=["/usr/local/bin/backup", "--check-access"],
        handoffs=[tasks.Handoff("url", "x-apple.systempreferences:com.apple.preference.security",
                                "Open the settings")])
    engine.verify_pending()            # later, on a schedule: closes what is now done

The pieces, each replaceable.

- ``TaskStore`` keeps tasks and their history. ``SqliteTaskStore`` is the default, one file.
- ``Verifier`` decides whether a task is done. ``CommandVerifier`` runs the task's verify command
  (a list of arguments, never a shell string) with a timeout, and exit 0 means done.
- A ``claude_human.notify`` notifier tells the person when a task opens.
- ``HandoffResolver`` orders the ways to reach the task for one touchpoint (``filter_chain``) and
  readies the chosen one. Plain steps are the floor.
- ``Authorizer`` says whether an actor may ask. A refusal is recorded, then raised as ``Refused``.

``claude_human.tasks.server`` serves the same engine over HTTP on 127.0.0.1 with a bearer token,
and ``claude-human task ...`` drives it from a shell.
"""
from .engine import (Allow, CommandVerifier, DefaultResolver, Refused, TaskEngine, decision,
                     filter_chain)
from .model import (FLOOR_KINDS, FLOOR_PREFERENCE, OUTCOMES, Authorizer, Handoff, HandoffResolver,
                    Task, TaskStore, Verifier, check_answer, entry_json, head, kind_of)
from .sqlite import SqliteTaskStore

__all__ = [
    "Allow", "Authorizer", "CommandVerifier", "DefaultResolver", "FLOOR_KINDS", "FLOOR_PREFERENCE",
    "Handoff", "HandoffResolver", "OUTCOMES", "Refused", "SqliteTaskStore", "Task", "TaskEngine", "TaskStore",
    "Verifier", "check_answer", "decision", "entry_json", "filter_chain", "head", "kind_of",
]
