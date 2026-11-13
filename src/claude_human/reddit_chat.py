"""Read Reddit chat's list of rooms from a browser tab a program controls, without opening a room.

The scripts here run inside a tab that shows Reddit chat, through whatever runs JavaScript in that tab
(`brave.js_in`, a DevTools session, a test page). Each one evaluates to a JSON string, so a caller that
gets only a string back parses it with the functions below. Nothing here clicks, types or sends.

- `URL` is the page to keep the tab on. It is the "new chat" page, which draws the list of rooms and
  opens none of them. `https://www.reddit.com/chat/` opens the newest room, and an open room is how
  Reddit marks its messages read, so a reader that only watches for new messages stays off it.
- `STATE` says what the page shows: the list of rooms (`signed_in`), Reddit's sign-in page
  (`signed_out`), or nothing yet (`loading`).
- `ROOMS` lists the rooms as the list draws them, newest first: the room's id, its name, the time the
  list shows for its last message, that message's text with its "<name>: " or "You: " in front, and
  the unread count on its badge. It answers null until the list has drawn its first row.
- `parse_rooms` turns that into rows, splitting off who wrote the last message. A row whose last
  message starts with "You: " was written by the signed-in account. `waiting` keeps the rooms whose last
  message came from someone else, which are the ones that wait on an answer.

The list holds the whole last message, even when the page shows it cut. Reddit draws the list with web
components inside shadow roots, so the scripts walk every open shadow root. Reddit changes its page
from time to time, and a script that finds nothing says so (null) rather than guess.
"""
from __future__ import annotations

import datetime as dt
import json
import re

#: the page to keep the tab on: it lists the rooms and opens none
URL = "https://www.reddit.com/chat/room/create"
#: how the list marks a last message the signed-in account wrote
MINE = "You: "

_WALK = r"""
function walk(root, f){root.querySelectorAll('*').forEach(function(el){f(el);if(el.shadowRoot)walk(el.shadowRoot,f);});}
function txt(root, sel){var e=root&&root.querySelector(sel);return e?(e.textContent||'').replace(/\s+/g,' ').trim():null;}
"""

STATE = "(function(){" + _WALK + r"""
var nav=false;walk(document,function(e){if(e.tagName==='RS-ROOMS-NAV')nav=true;});
if(nav)return JSON.stringify({state:'signed_in'});
if(/^\/login/.test(location.pathname)||document.querySelector('a[href*="/login"]')&&!document.querySelector('rs-app'))
return JSON.stringify({state:'signed_out'});
return JSON.stringify({state:'loading'});})()"""

ROOMS = "(function(){" + _WALK + r"""
var rows=[];walk(document,function(e){if(e.tagName!=='RS-ROOMS-NAV-ROOM')return;var r=e.shadowRoot||e;
var name=txt(r,'.room-name');var last=txt(r,'.last-message');if(!name&&!last)return;
var badge=txt(r,'.badges');rows.push({room:e.getAttribute('room'),name:name,time:txt(r,'.last-message-time'),
last:last,badge:badge});});
return JSON.stringify(rows.length?rows:null);})()"""


def _load(raw: str | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def parse_state(raw: str | None) -> str:
    """`STATE`'s answer: "signed_in", "signed_out" or "loading", and "unknown" for anything else."""
    got = _load(raw)
    state = got.get("state") if isinstance(got, dict) else None
    return state if state in ("signed_in", "signed_out", "loading") else "unknown"


def split_last(name: str | None, last: str | None) -> tuple[bool | None, str | None, str]:
    """Pure. (written by the signed-in account, who wrote it, the text) for one row's last message.

    "You: hello" is the account's own. "<name>: hello" is that person's; in a room with one other
    person that is the room's name, and in a group it is whoever the line names. A line with neither
    in front (a notice such as "Group created") has no writer: (None, None, the line)."""
    line = " ".join((last or "").split())
    if line.startswith(MINE):
        return True, None, line[len(MINE):]
    if name and line.startswith(f"{name}: "):
        return False, name, line[len(name) + 2:]
    head, sep, rest = line.partition(": ")
    if sep and head and len(head) <= 64 and " " not in head:
        return False, head, rest
    return None, None, line


_COUNT = re.compile(r"\d+")


def parse_rooms(raw: str | None) -> list[dict] | None:
    """`ROOMS`'s answer as rows, None when the list has not drawn. Each row is {room, name, time,
    from_me, sender, text, unread}: `time` is what the list shows ("12:40 PM", "Yesterday", "Oct 9"),
    `from_me` None when the line names no writer, and `unread` the badge's count, 0 without one."""
    got = _load(raw)
    if not isinstance(got, list):
        return None
    out = []
    for r in got:
        if not isinstance(r, dict) or not r.get("room"):
            continue
        from_me, sender, text = split_last(r.get("name"), r.get("last"))
        count = _COUNT.search(str(r.get("badge") or ""))
        out.append({"room": r["room"], "name": r.get("name"), "time": r.get("time"), "from_me": from_me,
                    "sender": sender, "text": text, "unread": int(count.group()) if count else 0})
    return out


def waiting(rows: list[dict] | None) -> list[dict]:
    """Pure. The rooms whose last message someone else wrote: each waits on an answer."""
    return [r for r in rows or [] if r.get("from_me") is False and r.get("text")]


_CLOCK = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>[AaPp][Mm])?$")
_MONTH_DAY = re.compile(r"^(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<d>\d{1,2})(?:,\s*(?P<y>\d{4}))?$")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), start=1)}


def shown_at(shown: str | None, now: dt.datetime) -> tuple[dt.datetime | None, bool]:
    """Pure. (when, today) for the time the list shows beside a room. A clock time ("12:40 PM", "14:05")
    is today at that time; "Yesterday" is the day before at midnight; a month and day ("Oct 9", "Oct 9,
    2025") is that day, in the year before when it would be later than `now`. `now` carries the zone
    the browser shows times in. (None, False) for anything else."""
    s = " ".join((shown or "").split())
    m = _CLOCK.match(s)
    if m:
        h, mi = int(m["h"]), int(m["m"])
        if m["ampm"]:
            h = h % 12 + (12 if m["ampm"].lower() == "pm" else 0)
        if h > 23 or mi > 59:
            return None, False
        return now.replace(hour=h, minute=mi, second=0, microsecond=0), True
    if s.lower() == "yesterday":
        return (now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), False
    m = _MONTH_DAY.match(s)
    if m and m["mon"].lower()[:3] in _MONTHS:
        year = int(m["y"]) if m["y"] else now.year
        try:
            when = now.replace(year=year, month=_MONTHS[m["mon"].lower()[:3]], day=int(m["d"]), hour=0,
                               minute=0, second=0, microsecond=0)
        except ValueError:
            return None, False
        if not m["y"] and when > now:
            when = when.replace(year=year - 1)
        return when, False
    return None, False
