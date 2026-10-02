"""Tell a person something, through a self-hosted ntfy server, with a file as the floor.

    from claude_human import notify

    n = notify.NtfyNotifier("https://ntfy.example.org/alerts", token=os.environ.get("NTFY_TOKEN"))
    ok, detail = n.notify("Backup failed", "The nightly dump exited 1.", priority="high",
                          link="myapp://task/42", tags=["warning"])

Every notifier returns ``(ok, detail)`` and never raises because a server was down, slow or
refused the message. ``detail`` says what happened in either case: the message id on success, the
reason on failure. A caller records both. A sender that swallows its own failure turns "nobody was
told" into "nobody knows whether anybody was told", which is worse than having no sender.

A successful send means the message left. It does not mean the person read it. Use ``poll`` to see
that the server holds it, and a receipt from the phone if you need to know it was opened.

Lessons this module keeps, each from a notifier that went wrong:

- Publish as JSON to the server root, never through ntfy's header form. Headers are encoded as
  latin-1, so a title with a check mark, a Greek word or an emoji raised ``UnicodeEncodeError`` and
  the alert never left. The JSON form carries every field as UTF-8.
- A loopback address is right for publishing from the server's own host, and wrong in anything the
  phone opens. A ``link``, ``attach`` or action URL on ``localhost`` makes the phone open itself.
  Such a message is refused with a reason (``allow_loopback_links=True`` turns this off).
- ntfy reads ``since`` as a Go duration with no day unit, so ``7d`` is a 400. ``poll`` turns days into
  hours before it asks.
- A test run must never reach a person. When ``CLAUDE_HUMAN_NOTIFY_HOLD`` is set (or ``hold`` is
  passed), ``NtfyNotifier`` sends nothing and answers ``(False, "held: ...")``, so the caller can
  still check which notifiers it chose.

Only the Python standard library is used.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

#: The environment variables ``from_env`` and the CLI read. The token and the password are read
#: only from the environment, never from an argument, so they stay out of the process list.
ENV_URL = "CLAUDE_HUMAN_NTFY_URL"
ENV_TOKEN = "CLAUDE_HUMAN_NTFY_TOKEN"
ENV_USER = "CLAUDE_HUMAN_NTFY_USER"
ENV_PASSWORD = "CLAUDE_HUMAN_NTFY_PASSWORD"
#: When set to anything but empty or "0", nothing is sent. For test runs.
ENV_HOLD = "CLAUDE_HUMAN_NOTIFY_HOLD"

#: ntfy's priority names and the numbers its JSON publish takes.
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}

#: ntfy's topic rule: letters, digits, underscore and dash, at most 64.
_TOPIC = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_TITLE_MAX = 250


@runtime_checkable
class Notifier(Protocol):
    """Anything that can tell a person something and say whether it left."""

    def notify(self, title: str, body: str, *, priority: str | int | None = None,
               link: str | None = None, tags: Iterable[str] = ()) -> tuple[bool, str]:
        ...


def priority_number(priority: str | int | None) -> int:
    """ntfy's priority as the number its JSON publish takes (1 to 5). None is the default, 3."""
    if priority is None:
        return 3
    if isinstance(priority, bool):
        raise ValueError(f"not a priority: {priority!r}")
    if isinstance(priority, int):
        if 1 <= priority <= 5:
            return priority
        raise ValueError(f"priority must be 1 to 5, not {priority}")
    key = str(priority).strip().lower()
    if key.isdigit():
        return priority_number(int(key))
    if key in PRIORITIES:
        return PRIORITIES[key]
    raise ValueError(f"unknown priority {priority!r} (one of {', '.join(PRIORITIES)} or 1 to 5)")


def split_topic_url(url: str) -> tuple[str, str]:
    """``https://host/path/topic`` -> (``https://host/path``, ``topic``). Raises ValueError when the
    URL is not http(s) or its last segment is not a valid topic name."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("the ntfy URL must be http(s)://host/topic")
    base, _, topic = parts.path.rstrip("/").rpartition("/")
    if not _TOPIC.match(topic):
        raise ValueError("the ntfy URL must end in a topic name (letters, digits, _ and -)")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, base, "", "")), topic


def is_loopback_url(url: str) -> bool:
    """Whether an http(s) URL points at this machine (localhost, 127.0.0.0/8, ::1, 0.0.0.0).
    Other schemes (an app's own ``myapp://`` links) are never loopback."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def go_since(since: str | int | float) -> str:
    """A ``since`` value ntfy accepts. Numbers are seconds. ``7d`` becomes ``168h`` because Go
    durations have no day unit and ntfy answers a day with 400. ``all``, a unix time and a
    message id pass through."""
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        return f"{int(since)}s"
    s = str(since).strip()
    m = re.fullmatch(r"(\d+)d", s)
    if m:
        return f"{int(m.group(1)) * 24}h"
    return s


def _auth_header(token: str | None, user: str | None, password: str | None) -> dict[str, str]:
    if token:
        return {"Authorization": f"Bearer {token}"}
    if user:
        pair = f"{user}:{password or ''}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    return {}


def _held(hold: bool | str | None) -> str:
    """The reason to hold a send, or "" to send it."""
    if hold:
        return hold if isinstance(hold, str) else "hold is set"
    env = os.environ.get(ENV_HOLD, "").strip()
    if env and env != "0":
        return f"{ENV_HOLD} is set"
    return ""


class NtfyNotifier:
    """Publish to one ntfy topic.

    ``url`` is the topic URL, ``https://host/topic``. Give either ``token`` (a bearer access token)
    or ``user`` and ``password`` (basic auth), or neither for an open topic. ``timeout`` bounds the
    whole request in seconds.
    """

    def __init__(self, url: str, *, token: str | None = None, user: str | None = None,
                 password: str | None = None, timeout: float = 10.0,
                 allow_loopback_links: bool = False, hold: bool | str | None = None):
        self.url = url
        self.token = token or None
        self.user = user or None
        self.password = password
        self.timeout = timeout
        self.allow_loopback_links = allow_loopback_links
        self.hold = hold

    def __repr__(self) -> str:   # the token never appears in a log line
        auth = "token" if self.token else ("basic" if self.user else "none")
        return f"NtfyNotifier(url={self.url!r}, auth={auth})"

    def payload(self, title: str, body: str, *, priority: str | int | None = None,
                link: str | None = None, tags: Iterable[str] = (), attach: str | None = None,
                actions: list[dict] | None = None, markdown: bool = False) -> tuple[str, dict]:
        """The server root and the JSON body a publish sends. Raises ValueError on bad input."""
        base, topic = split_topic_url(self.url)
        data: dict = {"topic": topic, "message": body or "", "priority": priority_number(priority)}
        if title:
            data["title"] = title[:_TITLE_MAX]
        tag_list = [str(t) for t in tags if str(t)]
        if tag_list:
            data["tags"] = tag_list
        if link:
            data["click"] = link
        if attach:
            data["attach"] = attach
        if actions:
            data["actions"] = list(actions)
        if markdown:
            data["markdown"] = True
        if not self.allow_loopback_links:
            opened = [("link", link), ("attach", attach)] + [
                (f"action {a.get('label', i)}", a.get("url")) for i, a in enumerate(actions or [])]
            for what, u in opened:
                if u and is_loopback_url(u):
                    raise ValueError(f"the {what} URL points at this machine, and the phone would "
                                     f"open itself; give an address the phone can reach")
        return base + "/", data

    def notify(self, title: str, body: str, *, priority: str | int | None = None,
               link: str | None = None, tags: Iterable[str] = (), attach: str | None = None,
               actions: list[dict] | None = None, markdown: bool = False) -> tuple[bool, str]:
        """Publish one message. ``link`` is where a tap lands (ntfy's click), ``attach`` a URL to a
        picture, ``actions`` ntfy's action buttons ({action, label, url, ...}). Returns (ok, detail)
        and never raises."""
        try:
            root, data = self.payload(title, body, priority=priority, link=link, tags=tags,
                                      attach=attach, actions=actions, markdown=markdown)
        except ValueError as e:
            return False, f"refused: {e}"
        held = _held(self.hold)
        if held:
            return False, f"held: {held}, nothing sent"
        headers = {"Content-Type": "application/json", **_auth_header(self.token, self.user, self.password)}
        req = urllib.request.Request(root, data=json.dumps(data).encode("utf-8"), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read(4096)
                if not 200 <= r.status < 300:
                    return False, f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {_error_text(e)}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            return False, f"{type(e).__name__}: {reason}"[:200]
        try:
            msg_id = json.loads(raw.decode("utf-8")).get("id", "")
        except (ValueError, AttributeError):
            msg_id = ""
        return True, f"sent id={msg_id}" if msg_id else "sent"

    def poll(self, since: str | int | float = "10m") -> tuple[bool, list[dict] | str]:
        """The messages the server holds on this topic since ``since``, oldest first. Returns
        (True, messages) or (False, reason), and never raises. Useful to prove a send arrived."""
        try:
            base, topic = split_topic_url(self.url)
        except ValueError as e:
            return False, f"refused: {e}"
        q = urllib.parse.urlencode({"poll": "1", "since": go_since(since)})
        req = urllib.request.Request(f"{base}/{topic}/json?{q}",
                                     headers=_auth_header(self.token, self.user, self.password))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {_error_text(e)}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, f"{type(e).__name__}: {getattr(e, 'reason', e)}"[:200]
        out = []
        for line in text.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("event", "message") == "message":
                out.append(m)
        return True, out


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read(400).decode("utf-8", "replace")
        return (json.loads(body).get("error") or body).strip()[:160]
    except (ValueError, AttributeError, OSError):
        return str(e.reason)[:160]


class FileNotifier:
    """Append each message as one JSON line to a file. Never needs a network, so it is the floor
    under every other notifier: what was meant for a person is at least written down."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()

    def notify(self, title: str, body: str, *, priority: str | int | None = None,
               link: str | None = None, tags: Iterable[str] = (), **_extra) -> tuple[bool, str]:
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "title": title, "body": body,
                 "priority": priority, "link": link, "tags": list(tags)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            return False, f"{type(e).__name__}: {e}"[:200]
        return True, f"written to {self.path}"


class FirstThatWorks:
    """Try each notifier in order and stop at the first that sends. Returns the last detail of
    every attempt, so a fallback to the file still says why the network one failed."""

    def __init__(self, *notifiers: Notifier):
        self.notifiers = notifiers

    def notify(self, title: str, body: str, *, priority: str | int | None = None,
               link: str | None = None, tags: Iterable[str] = ()) -> tuple[bool, str]:
        tried = []
        tags = list(tags)
        for n in self.notifiers:
            try:
                ok, detail = n.notify(title, body, priority=priority, link=link, tags=tags)
            except Exception as e:  # noqa: BLE001 - one broken notifier must not stop the next
                ok, detail = False, f"{type(e).__name__}: {e}"[:200]
            tried.append(f"{type(n).__name__}: {detail}")
            if ok:
                return True, "; ".join(tried)
        return False, "; ".join(tried) or "no notifier"


def from_env(url: str | None = None, *, token_env: str = ENV_TOKEN, **kwargs) -> NtfyNotifier:
    """An NtfyNotifier from ``CLAUDE_HUMAN_NTFY_URL`` (or ``url``), with the token from
    ``token_env`` or basic auth from ``CLAUDE_HUMAN_NTFY_USER`` and ``CLAUDE_HUMAN_NTFY_PASSWORD``.
    Raises ValueError when no URL is given anywhere."""
    url = url or os.environ.get(ENV_URL, "")
    if not url:
        raise ValueError(f"no ntfy URL (pass one or set {ENV_URL})")
    return NtfyNotifier(url, token=os.environ.get(token_env) or None,
                        user=os.environ.get(ENV_USER) or None,
                        password=os.environ.get(ENV_PASSWORD) or None, **kwargs)


def notify(url: str, title: str, body: str, **kwargs) -> tuple[bool, str]:
    """One message to one topic, with the token and basic auth read from the environment."""
    keys = ("priority", "link", "tags", "attach", "actions", "markdown")
    send = {k: kwargs.pop(k) for k in keys if k in kwargs}
    try:
        n = from_env(url, **kwargs)
    except ValueError as e:
        return False, f"refused: {e}"
    return n.notify(title, body, **send)


def watch_sender(notifier: Notifier, title: str = "watcher", **opts):
    """Adapt a notifier to the ``send`` argument of ``claude_ops.watch.run_source``, so a watcher
    that finds something new tells a person instead of a chat. The watcher's state advances only
    when this answers ``{"result": "sent"}``, which is the same rule as for a chat."""

    def send(target: str, text: str, *, transport: str = "", **_ignore) -> dict:
        ok, detail = notifier.notify(title, text, **opts)
        return {"result": "sent" if ok else "failed", "target": target, "detail": detail}

    return send
