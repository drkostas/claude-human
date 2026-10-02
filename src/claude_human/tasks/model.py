"""The shapes the task engine passes around, and the protocols a caller implements.

A ``Task`` is one thing a person was asked to do. A ``Handoff`` is one way to put the person in
front of it (a link, a window, plain steps). The protocols are what the engine calls, so a program
with its own records (a database, an issue tracker) can keep them and still use the engine.
"""
from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol, Sequence, Union, runtime_checkable

#: The kinds that are words, not a surface. A chain always ends in one when the touchpoint can show
#: it, and the head of a chain is the first handoff that is not one of these.
FLOOR_KINDS = ("steps",)

#: The preference the floor gets when nothing declared it, so it sorts after every real surface.
FLOOR_PREFERENCE = 9999


@dataclass
class Handoff:
    """One way to put a person in front of the thing a task is about.

    ``kind`` says what a touchpoint needs to show it (``url``, ``vnc``, ``steps``...). ``target`` is
    the address or the text. ``platform`` is ``any`` or the one platform it works on. ``prepare`` is
    a command (a list of arguments) that puts the surface into the right state before the person
    arrives, or None."""
    kind: str
    target: Optional[str]
    label: Optional[str] = None
    preference: int = 100
    platform: str = "any"
    prepare: Optional[list[str]] = None

    def to_json(self) -> dict:
        return {"kind": self.kind, "target": self.target, "label": self.label,
                "preference": self.preference, "platform": self.platform}


#: A chain entry is a Handoff, or a dict with at least ``kind`` and ``target`` (a store that reads
#: its chain from a database can hand its rows over as they are).
ChainEntry = Union[Handoff, dict]


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


@dataclass
class Task:
    """One request to a person, as the engine sees it.

    ``verify`` is the observation that closes the task. For the default verifier it is a list of
    arguments, run without a shell, and the task is done when it exits 0. A store with its own
    verifier may keep its own form here. ``extra`` carries fields a store wants on the JSON (a tier,
    a risk class), which the engine passes through untouched."""
    id: str
    capability: str
    subject: str
    reason: Optional[str] = None
    steps: Optional[str] = None
    verify: Any = None
    owner: Optional[str] = None
    actor: Optional[str] = None
    tier: str = "confirm"
    state: str = "open"
    opened_at: Optional[float] = None
    closed_at: Optional[float] = None
    outcome: Optional[str] = None
    handoffs: list[Handoff] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def waiting_s(self, now: Optional[float] = None) -> Optional[int]:
        if self.opened_at is None:
            return None
        end = self.closed_at if self.closed_at is not None else (now or time.time())
        return int(end - self.opened_at)

    def requires(self) -> str:
        """``remote`` when a surface other than the words exists, else ``physical``."""
        return "remote" if any(h.kind not in FLOOR_KINDS for h in self.handoffs) else "physical"

    def to_json(self) -> dict:
        """The task as the app reads it. ``intent`` is the id and ``verb`` the capability, the names
        the app was written against."""
        out = {"intent": self.id, "subject": self.subject, "verb": self.capability,
               "tier": self.tier, "reason": self.reason, "steps": self.steps,
               "waiting_s": self.waiting_s(), "requires": self.requires(), "owner": self.owner,
               "state": self.state, "opened_at": _iso(self.opened_at)}
        if self.state != "open":
            out["closed_at"] = _iso(self.closed_at)
            out["outcome"] = self.outcome
        out.update(self.extra)
        return out


@runtime_checkable
class TaskStore(Protocol):
    """Where tasks live. The engine never writes a task any other way.

    ``open_task`` must be single flight on its own: when an open task for the same (capability,
    subject) exists it returns that id and ``False``, even when two callers race. ``complete``
    returns False when the task was already closed, so a second observer cannot close it twice."""

    def find_open(self, capability: str, subject: str) -> Optional[str]: ...

    def open_task(self, capability: str, subject: str, reason: Optional[str],
                  owner: Optional[str], *, actor: Optional[str] = None,
                  **declared) -> tuple[str, bool]: ...

    def pending(self) -> list[Task]: ...

    def get(self, task_id: str) -> Optional[Task]: ...

    def complete(self, task_id: str, outcome: str, evidence: dict) -> bool: ...

    def withdraw(self, task_id: str, actor: Optional[str], reason: Optional[str]) -> dict: ...

    def comment(self, task_id: str, text: str, actor: Optional[str]) -> dict: ...

    def history(self, limit: int = 50) -> list[dict]: ...

    def record_refusal(self, capability: str, subject: str, actor: Optional[str],
                       reason: str) -> None: ...


@runtime_checkable
class Verifier(Protocol):
    """Decides whether a task is really done, by looking. Never by trusting a button."""

    def verify(self, task: Task) -> bool: ...


@runtime_checkable
class HandoffResolver(Protocol):
    """Orders the ways to reach a task for one touchpoint, and readies the chosen one."""

    def chain(self, task: Task, supports: Sequence[str], platform: str) -> list[ChainEntry]: ...

    def prepare(self, handoff: ChainEntry) -> tuple[Optional[bool], str]: ...


@runtime_checkable
class Authorizer(Protocol):
    """Whether ``actor`` may ask for ``verb`` on ``subject``. Answer a bool, or (bool, reason)."""

    def may_act(self, actor: Optional[str], subject: str, verb: str) -> Union[bool, tuple[bool, str]]: ...


#: what a check can answer. Only ``success`` says the task is done.
OUTCOMES = ("success", "failed", "unknown")


def check_answer(outcome: str, evidence) -> None:
    """Refuse an answer that cannot be recorded honestly. Raises ValueError.

    A success must say what was observed. "It is done" is a conclusion, and the observation is what
    makes it a fact, so a check that cannot name one answers ``unknown`` and the task stays open."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome {outcome!r} is not one of {', '.join(OUTCOMES)}")
    empty = evidence is None or (isinstance(evidence, str) and not evidence.strip()) or (
        isinstance(evidence, (dict, list, tuple)) and not evidence)
    if outcome == "success" and empty:
        raise ValueError("a success must say what was observed; record 'unknown' instead")


def kind_of(entry: ChainEntry) -> str:
    return entry.kind if isinstance(entry, Handoff) else entry.get("kind")


def entry_json(entry: ChainEntry) -> dict:
    return entry.to_json() if isinstance(entry, Handoff) else dict(entry)


def head(chain: Iterable[ChainEntry], floor: Sequence[str] = FLOOR_KINDS) -> Optional[ChainEntry]:
    """The surface to show: the first entry that is not the words. None when words are all there is.

    The touchpoint renders the head it is given and never ranks the chain again with a rule of its
    own, so every touchpoint agrees with the server."""
    return next((h for h in chain if kind_of(h) not in floor), None)
