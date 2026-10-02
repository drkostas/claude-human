"""The engine: ask once, tell the person, close only on an observation.

    engine = TaskEngine(store, notifier=notify.from_env(), link="myapp://task/{id}")
    task_id, new = engine.request("approve", "app://backup", "The backup needs Full Disk Access.",
                                  owner="backup-bot", steps="System Settings > Privacy > ...",
                                  verify=["/usr/local/bin/backup", "--check-access"])
    engine.verify_pending()          # closes every task whose verify command now exits 0

The rules it keeps, each from a task loop that went wrong somewhere.

- One open task per (capability, subject). A second request joins the first and nobody is told
  twice. A person who gets the same ask three times learns to ignore all of them.
- A task closes when an observation says it is done. Pressing "done" asks for the check, it does
  not answer it. A "done" that lies is worse than one that waits.
- A verify command that cannot run (missing, too slow, refused) means "not done", never "done".
- A refusal is recorded, not only raised, so "nobody was allowed" can be told apart from "nobody
  asked".
- The chain of handoffs is ordered by the server and filtered to what the touchpoint says it can
  show. Plain steps are the floor, so a touchpoint that can show words always has something.
"""
from __future__ import annotations

import subprocess
from typing import Iterable, Optional, Sequence

from .model import (FLOOR_KINDS, FLOOR_PREFERENCE, Authorizer, ChainEntry, Handoff,
                    HandoffResolver, Task, TaskStore, Verifier, entry_json, head, kind_of)


class Refused(Exception):
    """The authorizer said no. The refusal is already on the store's record when this is raised."""


def filter_chain(declared: Iterable[Handoff], supports: Sequence[str], platform: str,
                 steps: Optional[str] = None, floor: Sequence[str] = FLOOR_KINDS) -> list[Handoff]:
    """The ordered chain for one touchpoint.

    Keep a handoff when the touchpoint supports its kind, its platform is ``any`` or the
    touchpoint's own, and it has a target (an entry with nothing to open is a dead button). When the
    task has steps and the touchpoint can show a floor kind that nothing declared, add the steps as
    that kind, last. Sort by preference, keeping the declared order for ties."""
    supports = list(supports)
    kept = [h for h in declared
            if h.kind in supports and h.platform in ("any", platform) and h.target is not None]
    if steps:
        for k in floor:
            if k in supports and not any(h.kind == k for h in kept):
                kept.append(Handoff(k, steps, "Follow the steps", FLOOR_PREFERENCE, "any"))
    return sorted(kept, key=lambda h: h.preference)


def _run(argv: list[str], timeout: float) -> tuple[bool, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:g}s"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return False, f"could not run: {e}"
    return r.returncode == 0, f"exit {r.returncode}"


def _argv(value) -> Optional[list[str]]:
    """A command as a list of strings, or None. A string is refused: it would need a shell."""
    if isinstance(value, (list, tuple)) and value and all(isinstance(a, str) for a in value):
        return list(value)
    return None


class CommandVerifier:
    """Runs the task's verify command (a list of arguments, no shell) and answers whether it exited 0.

    A task with no command, or with a string where a list belongs, is never done. ``last`` keeps
    the detail of the most recent run for the caller to show."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.last = ""

    def verify(self, task: Task) -> bool:
        argv = _argv(task.verify)
        if argv is None:
            self.last = "no verify command (a list of arguments) on this task"
            return False
        ok, self.last = _run(argv, self.timeout)
        return ok


class DefaultResolver:
    """Orders the task's own handoffs with ``filter_chain`` and runs a handoff's ``prepare``."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def chain(self, task: Task, supports: Sequence[str], platform: str) -> list[ChainEntry]:
        return list(filter_chain(task.handoffs, supports, platform, task.steps))

    def prepare(self, handoff: ChainEntry) -> tuple[Optional[bool], str]:
        prep = handoff.prepare if isinstance(handoff, Handoff) else handoff.get("prepare")
        if not prep:
            return None, "nothing to prepare"
        argv = _argv(prep)
        if argv is None:
            return False, "prepare is not a list of arguments"
        ok, detail = _run(argv, self.timeout)
        return ok, ("prepared" if ok else f"not prepared: {detail}")


class Allow:
    """A small authorizer. ``actors`` and ``verbs`` are allow lists, and None allows any."""

    def __init__(self, actors: Optional[Iterable[str]] = None, verbs: Optional[Iterable[str]] = None):
        self.actors = set(actors) if actors is not None else None
        self.verbs = set(verbs) if verbs is not None else None

    def may_act(self, actor, subject, verb):
        if self.actors is not None and actor not in self.actors:
            return False, f"{actor or 'nobody'} may not ask for tasks"
        if self.verbs is not None and verb not in self.verbs:
            return False, f"{verb} is not a verb anyone may ask for"
        return True, ""


def decision(answer) -> tuple[bool, str]:
    """An authorizer's answer as (allowed, reason)."""
    if isinstance(answer, tuple):
        return bool(answer[0]), str(answer[1] if len(answer) > 1 else "")
    return bool(answer), ("" if answer else "refused")


class TaskEngine:
    """The generic half of a human task loop. Everything about state goes through ``store``.

    ``link`` is a format string with ``{id}``, the address a tap on the notification opens. Leave
    it None to send no link."""

    def __init__(self, store: TaskStore, *, verifier: Optional[Verifier] = None,
                 notifier=None, resolver: Optional[HandoffResolver] = None,
                 authorizer: Optional[Authorizer] = None, link: Optional[str] = None,
                 floor: Sequence[str] = FLOOR_KINDS):
        self.store = store
        self.verifier = verifier or CommandVerifier()
        self.notifier = notifier
        self.resolver = resolver or DefaultResolver()
        self.authorizer = authorizer
        self.link = link
        self.floor = tuple(floor)

    # ---- asking ----
    def request(self, capability: str, subject: str, reason: Optional[str] = None, *,
                owner: Optional[str] = None, actor: Optional[str] = None,
                **declared) -> tuple[str, bool]:
        """Ask a person for ``capability`` on ``subject``. Returns (task id, whether it is new).

        An open task for the same pair is joined and nobody is told again. Otherwise the authorizer
        is asked (a refusal is recorded, then raised as ``Refused``), the store opens the task, and
        the person is told. ``declared`` goes to the store as it is (steps, verify, handoffs...)."""
        existing = self.store.find_open(capability, subject)
        if existing is not None:
            return existing, False
        if self.authorizer is not None:
            allowed, why = decision(self.authorizer.may_act(actor, subject, capability))
            if not allowed:
                self.store.record_refusal(capability, subject, actor, why)
                raise Refused(why)
        task_id, new = self.store.open_task(capability, subject, reason, owner, actor=actor,
                                            **declared)
        if new:
            task = self.store.get(task_id)
            if task is not None:
                self.announce(task)
        return task_id, new

    def notice(self, task: Task) -> tuple[str, str, dict]:
        """The title, body and options of the notification for a new task."""
        body = "\n".join(x for x in (task.subject, task.reason, task.steps) if x)
        opts: dict = {"tags": [task.capability]}
        if self.link:
            opts["link"] = self.link.format(id=task.id)
        return f"Needs you: {task.capability}", body, opts

    def announce(self, task: Task):
        """Tell the person about a new task. Returns the notifier's (ok, detail), or None when there
        is no notifier. A notifier that raises is reported as (False, reason), never passed on."""
        if self.notifier is None:
            return None
        title, body, opts = self.notice(task)
        try:
            ok, detail = self.notifier.notify(title, body, **opts)
        except Exception as e:                                          # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        # both halves are kept when the store can hold them: "nobody was told" must not turn
        # into "nobody knows whether anybody was told"
        record = getattr(self.store, "record", None)
        if callable(record):
            try:
                record(task.id, "task.notify", "success" if ok else "failure", {"detail": detail})
            except Exception:                                           # noqa: BLE001
                pass
        return ok, detail

    # ---- closing ----
    def evidence(self, task: Task) -> dict:
        return {"verified_by": task.verify}

    def verify_pending(self, only: Optional[str] = None) -> list[Task]:
        """Observe every open task that names a verify command, and close the ones that are done.

        Returns the tasks closed in this pass. ``only`` limits the pass to one task id. A task the
        verifier does not confirm stays open, and nothing is written for it."""
        closed = []
        for task in self.store.pending():
            if only is not None and str(task.id) != str(only):
                continue
            if not task.verify:
                continue
            if not self.verifier.verify(task):
                continue
            if self.store.complete(task.id, "success", self.evidence(task)):
                closed.append(task)
        return closed

    def done(self, task_id: str) -> dict:
        """What a "done" button does: run the check for this one task and say what it saw."""
        task = self.store.get(task_id)
        if task is None:
            return {"done": False, "still_pending": False, "detail": "no such task"}
        if not task.is_open:
            return {"done": task.outcome == "success", "still_pending": False,
                    "detail": f"already closed ({task.outcome})"}
        if self.verify_pending(only=task_id):
            return {"done": True, "still_pending": False, "detail": "Checked, and it is done."}
        seen = getattr(self.verifier, "last", "")
        return {"done": False, "still_pending": True,
                "detail": "Checked, and it is not done yet." + (f" ({seen})" if seen else "")}

    # ---- showing ----
    def chain(self, task: Task, supports: Sequence[str] = ("steps",),
              platform: str = "any") -> list[ChainEntry]:
        return list(self.resolver.chain(task, list(supports), platform))

    def card(self, task: Task, supports: Sequence[str] = ("steps",), platform: str = "any") -> dict:
        """The task as a touchpoint renders it: its fields, its chain, and the head of the chain."""
        chain = self.chain(task, supports, platform)
        out = task.to_json()
        out["handoffs"] = [entry_json(h) for h in chain]
        h = head(chain, self.floor)
        out["head"] = entry_json(h) if h is not None else None
        return out

    def pending_cards(self, supports: Sequence[str] = ("steps",), platform: str = "any") -> list[dict]:
        return [self.card(t, supports, platform) for t in self.store.pending()]

    def open(self, task_id: str, supports: Sequence[str] = ("steps",), platform: str = "any") -> dict:
        """Ready the head of the chain for the person. ``prepared`` is None when nothing needed it,
        False when it could not be done (and ``detail`` says why)."""
        task = self.store.get(task_id)
        if task is None or not task.is_open:
            return {"kind": None, "target": None, "prepared": False, "detail": "no open task"}
        chain = self.chain(task, supports, platform)
        h = head(chain, self.floor)
        if h is None:
            return {"kind": kind_of(chain[0]) if chain else None,
                    "target": entry_json(chain[0]).get("target") if chain else None,
                    "prepared": None, "detail": "only steps, nothing to open"}
        prepared, detail = self.resolver.prepare(h)
        j = entry_json(h)
        return {"kind": j.get("kind"), "target": j.get("target"), "prepared": prepared,
                "detail": detail}
