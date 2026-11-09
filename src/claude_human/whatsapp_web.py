"""Read WhatsApp Web (web.whatsapp.com) from a browser tab a program controls, and link it to a phone
with a code instead of a QR code.

The scripts here run inside a tab that shows WhatsApp Web, through whatever runs JavaScript in that tab
(`brave.js_in`, a DevTools session, a test page). Each one evaluates to a JSON string, so a caller that
gets only a string back parses it with the functions below. Nothing here sends a message, and nothing
types a login code: linking with a phone number puts a code on the page, and the person types it into
WhatsApp on their own phone, which asks for their fingerprint or PIN.

- `STATE` says what the page shows: the chat list (`signed_in`), the QR code (`signed_out`), the form
  that takes the phone number (`number`), a link code (`code`, with the code), WhatsApp open in
  another window of the same browser (`elsewhere`), or nothing yet (`loading`). Signed out is also how
  an ended link shows.
- `USE_HERE` presses "Use here" when the page is `elsewhere`, which takes the session from the other
  window. WhatsApp Web runs in one window of a browser at a time, and shows that page in every other
  one; it shows it only while the browser is linked.
- `LINK_BY_NUMBER` presses "Log in with phone number" (older pages say "Link with phone number"), and
  `fill_number_js(number)` puts the number in the form and presses Next. The page then shows an
  8-character code, which `STATE` reads.
- `CHATS` lists the chat list's rows, `open_chat_js(title)` opens the chat with that exact title (WhatsApp
  opens a chat only on a click, so this presses that one row and nothing else), and `MESSAGES` reads the
  messages the open chat has loaded. `SCROLL_UP` asks for older ones. `CLOSE_CHAT` closes the open chat
  again, so the messages that arrive later are not shown in it.
- `parse_messages` turns them into rows: WhatsApp's own message id, the chat's id, whether the account
  sent it, the sender, the text and the time. The time comes from the line WhatsApp puts before each
  message that can be copied ("[21:03, 25/10/2026] Alice: "), whose order of day and month follows the
  browser's language, so the caller says which (`day_first`).

WhatsApp changes its page often. Each script looks for a thing in two or three ways, from the most
specific to the most general, and says "unknown" rather than guess.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

#: the page
URL = "https://web.whatsapp.com/"

# the helpers every script below starts with: a visible test, and finding a control by its words
_HELPERS = r"""
function vis(e){if(!e)return false;var r=e.getBoundingClientRect();var s=getComputedStyle(e);
return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';}
function words(e){return (e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();}
function byWords(list){var want=list.map(function(w){return w.toLowerCase();});
function hit(e){return vis(e)&&want.indexOf(words(e).toLowerCase())>=0;}
var ctl=document.querySelectorAll('a,button,[role=button],[role=link]');
for(var i=0;i<ctl.length;i++){if(hit(ctl[i]))return ctl[i];}
var all=document.querySelectorAll('div,span');var best=null;
for(i=0;i<all.length;i++){var e=all[i];if(!hit(e))continue;
var inner=false;for(var k=0;k<e.children.length;k++){if(hit(e.children[k])){inner=true;break;}}
if(!inner){best=e;break;}}return best;}
function phoneInput(){var i=document.querySelector('input[aria-label*="phone number" i]')||document.querySelector('input[type=tel]');
return i&&vis(i)?i:null;}
function chatList(){return document.querySelector('#pane-side')||document.querySelector('[aria-label="Chat list"]')
||document.querySelector('[role=grid][aria-label]');}
function linkCode(){var a=document.querySelector('[data-link-code]');
if(a){var v=(a.getAttribute('data-link-code')||'').replace(/[^A-Za-z0-9]/g,'');if(v.length===8)return v;}
var all=document.querySelectorAll('div,span,p');for(var i=0;i<all.length;i++){var e=all[i];if(!vis(e))continue;
var t=words(e).replace(/[\s-]/g,'');if(t.length===8&&/^[A-Z0-9]{8}$/.test(t)){
var near=words(e.parentElement||e).toLowerCase()+' '+words((e.parentElement||e).parentElement||e).toLowerCase();
if(near.indexOf('code')>=0||document.body.innerText.toLowerCase().indexOf('enter code')>=0)return t;}}
return null;}
"""

#: what the page shows now, as JSON: {"state": "signed_in"|"code"|"number"|"signed_out"|"elsewhere"|"loading", "code",
#: "by_number", "qr"}; "number" is the form that takes the phone number, and "qr" says the QR code has
#: loaded. Press "Log in with phone number" only after it has: pressed while the QR still loads, the
#: page stayed as it was (measured on the live page, 2026-10-25, where the QR took 13 seconds)
STATE = "(function(){" + _HELPERS + r"""
if(chatList())return JSON.stringify({state:'signed_in',code:null,by_number:false,qr:false});
if(byWords(['Use here']))return JSON.stringify({state:'elsewhere',code:null,by_number:false,qr:false});
var code=linkCode();if(code)return JSON.stringify({state:'code',code:code,by_number:true,qr:false});
if(phoneInput())return JSON.stringify({state:'number',code:null,by_number:true,qr:false});
var link=byWords(['Log in with phone number','Link with phone number']);
var qr=!!(document.querySelector('canvas[aria-label]')||document.querySelector('[data-ref]'));
if(qr||link)return JSON.stringify({state:'signed_out',code:null,by_number:!!link,qr:qr});
return JSON.stringify({state:'loading',code:null,by_number:false,qr:false});
})()"""

#: press "Log in with phone number" on the signed-out page, as JSON: {"pressed": bool}
LINK_BY_NUMBER = "(function(){" + _HELPERS + r"""
var e=byWords(['Log in with phone number','Link with phone number']);if(!e)return JSON.stringify({pressed:false});
e.click();return JSON.stringify({pressed:true});
})()"""

#: press "Use here" when WhatsApp is open in another window of this browser, as JSON: {"pressed": bool}.
#: The page said "WhatsApp is open in another window. Click "Use here" to use WhatsApp in this window"
#: (measured on the live page, 2026-10-26), and the chat list showed 26 seconds after the press
USE_HERE = "(function(){" + _HELPERS + r"""
var e=byWords(['Use here']);if(!e)return JSON.stringify({pressed:false});
e.click();return JSON.stringify({pressed:true});
})()"""

#: an international number: a leading +, the country code and the rest, spaces allowed. The form keeps
#: the whole number in one field ("+30 " is already in it), so a number without its + is read with the
#: wrong country: "5550000000" became Brazil's +55 500 00 000 (measured on the live page, 2026-10-25)
_NUMBER = re.compile(r"\+[0-9][0-9 ]{6,18}[0-9]")


def fill_number_js(number: str) -> str:
    """The script that puts `number` (international, with its leading + and country code, such as
    "+15550000000") in the phone number form and presses Next, as JSON: {"filled": bool, "next": bool}.
    The form picks the country from the number. A value that is not such a number is refused here, so
    it never reaches the page."""
    if not _NUMBER.fullmatch(number or ""):
        raise ValueError("not an international phone number: a leading +, then digits and spaces")
    return "(function(){" + _HELPERS + r"""
var n=""" + json.dumps(number) + r""";
var inp=phoneInput();
if(!inp){var ins=document.querySelectorAll('input[type=text]');for(var i=0;i<ins.length;i++){if(vis(ins[i])){inp=ins[i];break;}}}
if(!inp)return JSON.stringify({filled:false,next:false});
var set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;inp.focus();set.call(inp,n);
inp.dispatchEvent(new Event('input',{bubbles:true}));inp.dispatchEvent(new Event('change',{bubbles:true}));
var next=byWords(['Next']);if(next)next.click();
return JSON.stringify({filled:true,next:!!next});
})()"""


#: the chat list's rows, as JSON: [{"title", "time", "preview", "unread"}]
CHATS = "(function(){" + _HELPERS + r"""
var list=chatList();if(!list)return JSON.stringify(null);
var rows=list.querySelectorAll('[role=listitem],[role=row]');var out=[];
for(var i=0;i<rows.length;i++){var r=rows[i];var ts=r.querySelectorAll('span[title]');if(!ts.length)continue;
var title=ts[0].getAttribute('title');var prev=ts.length>1?ts[ts.length-1].getAttribute('title'):null;
var time=null;var leaves=r.querySelectorAll('div,span');for(var j=0;j<leaves.length;j++){var l=leaves[j];
if(l.children.length)continue;var lt=words(l);if(lt&&lt!==title&&lt.length<=12&&
/^(\d{1,2}[:.]\d{2}(\s?[ap]\.?m\.?)?|\d{1,2}[\/.\-]\d{1,2}[\/.\-]\d{2,4}|yesterday|today|[a-z]+day)$/i.test(lt)){time=lt;break;}}
var u=r.querySelector('[aria-label*="unread" i]');var n=u?parseInt((u.getAttribute('aria-label').match(/\d+/)||['0'])[0],10):0;
out.push({title:title,time:time,preview:prev,unread:n});}
return JSON.stringify(out);
})()"""


def open_chat_js(title: str) -> str:
    """The script that opens the chat whose title is exactly `title`, as JSON: {"opened": bool}. It
    presses that one row and nothing else, and finds nothing for a title that is not in the list.

    WhatsApp Web ignores a bare mousedown, mouseup and click on a row: it listens for pointer events
    on the element under the pointer. So the row is scrolled into view, and the element at its centre
    gets the whole sequence a real mouse sends (pointerover, mouseover, mousemove, pointerdown,
    mousedown, pointerup, mouseup, click), from a primary mouse pointer with the button held on the
    downs. "opened" only says the row was pressed; read ``OPEN_TITLE`` to see that the chat opened."""
    return "(function(){" + _HELPERS + r"""
var want=""" + json.dumps(title) + r""";var list=chatList();if(!list)return JSON.stringify({opened:false});
var spans=list.querySelectorAll('span[title]');for(var i=0;i<spans.length;i++){
if(spans[i].getAttribute('title')!==want)continue;var row=spans[i].closest('[role=listitem],[role=row]')||spans[i];
if(row.scrollIntoView)row.scrollIntoView({block:'center'});
var b=row.getBoundingClientRect();var x=b.left+b.width/2,y=b.top+b.height/2;
var at=document.elementFromPoint(x,y);var el=(at&&row.contains(at))?at:row;
var P=window.PointerEvent||MouseEvent;
['pointerover','mouseover','mousemove','pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t){
var down=t==='pointerdown'||t==='mousedown';
var o={bubbles:true,cancelable:true,view:window,clientX:x,clientY:y,button:0,buttons:down?1:0,
pointerId:1,pointerType:'mouse',isPrimary:true};
el.dispatchEvent(t.indexOf('pointer')===0?new P(t,o):new MouseEvent(t,o));});
return JSON.stringify({opened:true});}
return JSON.stringify({opened:false});
})()"""


#: the title of the chat that is open, as JSON (a string, or null)
OPEN_TITLE = r"""(function(){var h=document.querySelector('#main header');if(!h)return JSON.stringify(null);
var t=h.querySelector('span[title]')||h.querySelector('span[dir=auto]');
return JSON.stringify(t?(t.getAttribute('title')||t.innerText||'').trim():null);})()"""


def open_title_js(title: str) -> str:
    """True, as JSON, when the open chat's header shows `title`, else false (no chat open, or another).

    Any span of the header counts when its title attribute or its whole text equals `title`, both
    with whitespace folded (no-break spaces too). Never a part of one, so a member named in a group's
    member list is not the group. It does not guess which span is the name: a group's header lists
    its members in a titled span before the name, and ``OPEN_TITLE``, which takes the first titled
    span, reads that list instead of the group."""
    return ("(function(){function fold(x){return String(x).replace(/\\u00a0/g,' ').split(/\\s+/)"
            ".filter(function(w){return w;}).join(' ');}var want=fold(" + json.dumps(title) + ");"
            "var h=document.querySelector('#main header');if(!h)return JSON.stringify(false);"
            "var s=h.querySelectorAll('span');for(var i=0;i<s.length;i++){var a=s[i].getAttribute('title');"
            "if(a!==null&&fold(a)===want)return JSON.stringify(true);"
            "if(fold(s[i].innerText||s[i].textContent||'')===want)return JSON.stringify(true);}"
            "return JSON.stringify(false);})()")

#: the messages the open chat has loaded, oldest first, as JSON: [{"id", "raw", "text", "kind"}]. A message
#: is the outermost element in the open chat with a `data-id`. Its id is either WhatsApp's long form
#: ("false_<chat>_<message>", which says who sent it and in which chat) or a bare message id of letters
#: and digits, which newer pages use; a bare id counts only on an element that holds a copyable line
#: (`data-pre-plain-text`) or a message container (`data-testid="msg-container"`)
MESSAGES = r"""(function(){var main=document.querySelector('#main');if(!main)return JSON.stringify(null);
var seen={};var out=[];var els=main.querySelectorAll('[data-id]');
for(var i=0;i<els.length;i++){var e=els[i];var id=e.getAttribute('data-id');if(!id||seen[id])continue;
if(e.parentElement&&e.parentElement.closest('[data-id]'))continue;
var c=e.querySelector('[data-pre-plain-text]');
if(!/^(true|false)_/.test(id)&&!(/^[0-9A-Za-z]{10,64}$/.test(id)&&(c||e.querySelector('[data-testid="msg-container"]'))))continue;
seen[id]=1;var raw=c?c.getAttribute('data-pre-plain-text'):null;
var s=e.querySelector('span.selectable-text')||c;var text=s?(s.innerText||'').trim():'';
var kind=text?'text':'other';if(!text){var l=e.querySelector('[aria-label]');if(l)kind=(l.getAttribute('aria-label')||'other').slice(0,40);}
out.push({id:id,raw:raw,text:text,kind:kind});}
return JSON.stringify(out);})()"""

#: whether the browser writes a date with its day first (25/10), as JSON true or false, or null when
#: it puts the year first; WhatsApp's line before a message follows the browser's language, so this is
#: what parse_messages' `day_first` should be
DAY_FIRST = r"""(function(){try{var p=new Intl.DateTimeFormat(undefined,{day:'numeric',month:'numeric',year:'numeric'})
.formatToParts(new Date(2026,9,5));var order=p.filter(function(x){return x.type==='day'||x.type==='month'||x.type==='year';})
.map(function(x){return x.type;});if(order[0]==='year')return JSON.stringify(null);
return JSON.stringify(order.indexOf('day')<order.indexOf('month'));}catch(e){return JSON.stringify(null);}})()"""

#: close the open chat, as JSON: {"closed": bool, "pressed": bool}. "closed" is true when no chat was
#: open. Otherwise it presses Escape, which closes the open chat in WhatsApp Web, and reloads the page
#: when a chat still shows a second and a half later, since a page that has just loaded has none open.
#: A chat left open shows each message that arrives, and an open chat is how WhatsApp marks messages read
CLOSE_CHAT = r"""(function(){if(!document.querySelector('#main'))return JSON.stringify({closed:true,pressed:false});
var t=document.activeElement||document.body;['keydown','keyup'].forEach(function(k){
t.dispatchEvent(new KeyboardEvent(k,{key:'Escape',code:'Escape',keyCode:27,which:27,bubbles:true,cancelable:true}));});
setTimeout(function(){if(document.querySelector('#main'))location.reload();},1500);
return JSON.stringify({closed:false,pressed:true});})()"""

#: scroll the open chat to its top, so WhatsApp loads older messages, as JSON: {"scrolled": bool}
SCROLL_UP = r"""(function(){var main=document.querySelector('#main');if(!main)return JSON.stringify({scrolled:false});
var m=main.querySelector('[data-id]');var p=m;while(p&&p!==main){var s=getComputedStyle(p);
if(p.scrollHeight>p.clientHeight&&/(auto|scroll)/.test(s.overflowY)){p.scrollTop=0;return JSON.stringify({scrolled:true});}
p=p.parentElement;}return JSON.stringify({scrolled:false});})()"""


#: move the open chat up by one step, so the messages above the ones drawn are drawn next, as JSON:
#: {"found", "depth", "client_height", "scroll_height", "top_before", "top_after"}, numbers only. The
#: chat is scrolled by the nearest element above its oldest drawn message whose content is taller than
#: it and that scrolls ("depth" is how many levels above that message it is). A page that draws only the
#: messages in view draws few of them in a tab that is not shown, so the step is the height of what is
#: drawn (at most 80% of the scrolled area), and the next window joins this one with nothing skipped
SCROLL_STEP = r"""(function(){var main=document.querySelector('#main');var no={found:false};if(!main)return JSON.stringify(no);
var line=main.querySelector('[data-pre-plain-text]')||main.querySelector('[data-id]');if(!line)return JSON.stringify(no);
var p=line.parentElement,d=1;while(p&&p!==main){var s=getComputedStyle(p);
if(p.scrollHeight>p.clientHeight+1&&/(auto|scroll)/.test(s.overflowY||''))break;p=p.parentElement;d++;}
if(!p||p===main)return JSON.stringify(no);
var drawn=0,els=p.querySelectorAll('[data-id]');for(var i=0;i<els.length;i++){var e=els[i];
if(e.parentElement&&e.parentElement.closest('[data-id]'))continue;drawn+=e.offsetHeight||0;}
var step=Math.max(1,Math.min(Math.round(p.clientHeight*0.8),drawn||Math.round(p.clientHeight*0.8)));
var before=Math.round(p.scrollTop);p.scrollTop=Math.max(0,before-step);
return JSON.stringify({found:true,depth:d,client_height:Math.round(p.clientHeight),
scroll_height:Math.round(p.scrollHeight),top_before:before,top_after:Math.round(p.scrollTop)});})()"""

def parse_day_first(raw: str | None, default: bool = True) -> bool:
    """`DAY_FIRST`'s answer as a bool; `default` when the browser puts the year first or did not say
    (a date with its year first reads the same either way)."""
    got = _load(raw)
    return got if isinstance(got, bool) else default


def _load(raw: str | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def parse_state(raw: str | None) -> dict:
    """`STATE`'s answer as a dict; {"state": "unknown"} for an answer that is not one."""
    got = _load(raw)
    if not isinstance(got, dict) or got.get("state") not in ("signed_in", "code", "number", "signed_out",
                                                              "elsewhere", "loading"):
        return {"state": "unknown", "code": None, "by_number": False, "qr": False}
    code = got.get("code")
    if isinstance(code, str) and len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    return {"state": got["state"], "code": code if got["state"] == "code" else None,
            "by_number": bool(got.get("by_number")), "qr": bool(got.get("qr"))}


def parse_chats(raw: str | None) -> list[dict] | None:
    """`CHATS`'s answer as a list, None when no chat list shows."""
    got = _load(raw)
    if not isinstance(got, list):
        return None
    return [{"title": r.get("title"), "time": r.get("time"), "preview": r.get("preview"),
             "unread": int(r.get("unread") or 0)} for r in got if isinstance(r, dict) and r.get("title")]


_BARE_ID = re.compile(r"^[0-9A-Za-z]{10,64}$")


def split_id(data_id: str) -> dict | None:
    """WhatsApp's message id on the page as {"from_me", "chat", "message", "participant"}. The long form
    ("false_15550000000@c.us_3EB0A1B2C3", with the participant at the end in a group) gives all four. A
    bare message id ("3EB0A1B2C3D4E5F6A7B8C9"), which newer pages use, gives only "message": who sent it
    and the chat are None, never a guess. None for any other form, and for an empty id."""
    data_id = data_id or ""
    if _BARE_ID.match(data_id):
        return {"from_me": None, "chat": None, "message": data_id, "participant": None}
    parts = data_id.split("_")
    if len(parts) < 3 or parts[0] not in ("true", "false") or "@" not in parts[1]:
        return None
    return {"from_me": parts[0] == "true", "chat": parts[1], "message": parts[2],
            "participant": parts[3] if len(parts) > 3 and "@" in parts[3] else None}

_PREFIX = re.compile(r"^\[(?P<time>[^,\]]+),\s*(?P<date>[^\]]+)\]\s*(?P<sender>.*?):\s*$", re.S)
_TIME = re.compile(r"^(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:[:.](?P<s>\d{2}))?\s*(?P<ampm>[ap]\.?\s?m\.?)?$", re.I)
_DATE = re.compile(r"^(?P<a>\d{1,4})[/.\-](?P<b>\d{1,2})[/.\-](?P<c>\d{1,4})$")


def parse_prefix(prefix: str | None, *, day_first: bool = True) -> tuple[dt.datetime | None, str | None]:
    """(when, sender) from the line WhatsApp puts before a message that can be copied, such as
    "[21:03, 25/10/2026] Alice: " or "[9:03 PM, 10/25/2026] Alice: ". The time is naive, in the
    browser's own clock. `day_first` says the order of a date such as 05/10/2026, which only the
    browser's language decides; a date with a year first, or a part over 12, decides by itself. A part
    that does not read is None."""
    m = _PREFIX.match(prefix or "")
    if not m:
        return None, None
    sender = m.group("sender").strip() or None
    t, d = _TIME.match(m.group("time").strip()), _DATE.match(m.group("date").strip())
    if not t or not d:
        return None, sender
    hour, minute, second = int(t.group("h")), int(t.group("m")), int(t.group("s") or 0)
    ampm = (t.group("ampm") or "").lower().replace(".", "").replace(" ", "")
    if ampm:
        if hour > 12:
            return None, sender
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    a, b, c = d.group("a"), d.group("b"), d.group("c")
    if len(a) == 4:                              # 2026-10-25
        year, month, day = int(a), int(b), int(c)
    else:
        year = int(c) + (2000 if len(c) == 2 else 0)
        x, y = int(a), int(b)
        if x > 12:
            day, month = x, y
        elif y > 12:
            month, day = x, y
        else:
            day, month = (x, y) if day_first else (y, x)
    try:
        return dt.datetime(year, month, day, hour, minute, second), sender
    except ValueError:
        return None, sender


def parse_messages(raw: str | None, *, day_first: bool = True, tz: dt.tzinfo | None = None,
                   chat: str | None = None) -> list[dict] | None:
    """`MESSAGES`'s answer as rows, oldest first: {"id", "chat", "from_me", "sender", "text", "sent_at",
    "kind", "raw"}. `id` is WhatsApp's message id and `chat` the chat's id, both from the page. A bare
    id names no chat, so its row takes `chat` (the chat the caller opened), and its `from_me` is None.
    `sent_at` is ISO 8601, with `tz` (the browser's zone) when given, and None for a message with no
    copyable line (a photo without a caption, a deleted message). None when no chat is open."""
    got = _load(raw)
    if not isinstance(got, list):
        return None
    rows = []
    for r in got:
        if not isinstance(r, dict):
            continue
        ids = split_id(str(r.get("id") or ""))
        if ids is None:
            continue
        when, sender = parse_prefix(r.get("raw"), day_first=day_first)
        if when is not None and tz is not None:
            when = when.replace(tzinfo=tz)
        rows.append({"id": ids["message"], "chat": ids["chat"] or chat, "from_me": ids["from_me"],
                     "sender": sender, "text": r.get("text") or "",
                     "sent_at": when.isoformat() if when else None,
                     "kind": r.get("kind") or "other", "raw": r.get("raw")})
    return rows


#: how many steps' gains `collect` reports
GAINS_KEPT = 20
#: why `collect` stopped: enough messages, the chat's top reached, steps that brought nothing new, no
#: element that scrolls, the most steps taken, or no chat open
STOPS = ("enough", "top", "idle", "no_scroller", "max_steps", "no_chat")


def _merge(have: list[dict], window: list[dict]) -> int:
    """Add `window`'s rows that `have` lacks, by id, in place: those before the first row `have`
    already holds are older and go in front, the rest after. Returns how many were added."""
    known = {r["id"] for r in have}
    new = [r for r in window if r["id"] not in known]
    if not new:
        return 0
    first = next((i for i, r in enumerate(window) if r["id"] in known), len(window))
    older = [r for r in window[:first] if r["id"] not in known]
    newer = [r for r in window[first:] if r["id"] not in known]
    have[:0] = older
    have.extend(newer)
    return len(new)


def collect(js, want: int, *, max_steps: int = 20, wait_s: float = 2.0, idle_steps: int = 3,
            sleep=time.sleep) -> tuple[str | None, dict]:
    """The open chat's messages read across scroll steps and merged by id, as (`MESSAGES`-style JSON,
    oldest first, or None when no chat is open; how it collected). A page that draws only the messages
    in view holds few of them at once, so one read of `MESSAGES` misses the rest. This reads, moves
    up one step (`SCROLL_STEP`), waits `wait_s` for the page to draw, and reads again, until there are
    `want` messages, the chat's top is reached, `idle_steps` steps in a row brought nothing new, or
    `max_steps` were taken. `js` runs a script in the tab and returns its string.

    The second value has numbers only, besides "stopped" (one of STOPS): "first" (messages in the first
    read), "scroller" (1 when an element that scrolls was found), its "depth" above the oldest drawn
    message, "client_height" and "scroll_height", "steps" taken and "gained", the new messages each
    step brought (the first GAINS_KEPT steps)."""
    how = {"first": 0, "scroller": 0, "depth": None, "client_height": None, "scroll_height": None,
           "steps": 0, "gained": [], "stopped": "no_chat"}
    got = _load(js(MESSAGES))
    if not isinstance(got, list):
        return None, how
    have: list[dict] = []
    _merge(have, [r for r in got if isinstance(r, dict) and r.get("id")])
    how["first"] = len(have)
    idle = 0
    while True:
        if len(have) >= want:
            how["stopped"] = "enough"
            break
        if how["steps"] >= max_steps:
            how["stopped"] = "max_steps"
            break
        step = _load(js(SCROLL_STEP))
        if not isinstance(step, dict) or not step.get("found"):
            how["stopped"] = "no_scroller"
            break
        how["scroller"] = 1
        for k in ("depth", "client_height", "scroll_height"):
            v = step.get(k)
            how[k] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
        how["steps"] += 1
        moved = step.get("top_after") != step.get("top_before")
        sleep(wait_s)
        got = _load(js(MESSAGES))
        n = _merge(have, [r for r in got if isinstance(r, dict) and r.get("id")]) if isinstance(got, list) else 0
        if len(how["gained"]) < GAINS_KEPT:
            how["gained"].append(n)
        idle = 0 if n else idle + 1
        if not moved and not n:
            how["stopped"] = "top"
            break
        if idle >= idle_steps:
            how["stopped"] = "idle"
            break
    return json.dumps(have), how
