"""Read WhatsApp Web (web.whatsapp.com) from a browser tab a program controls, and link it to a phone
with a code instead of a QR code.

The scripts here run inside a tab that shows WhatsApp Web, through whatever runs JavaScript in that tab
(`brave.js_in`, a DevTools session, a test page). Each one evaluates to a JSON string, so a caller that
gets only a string back parses it with the functions below. Nothing here sends a message, and nothing
types a login code: linking with a phone number puts a code on the page, and the person types it into
WhatsApp on their own phone, which asks for their fingerprint or PIN.

- `STATE` says what the page shows: the chat list (`signed_in`), the QR code (`signed_out`), the form
  that takes the phone number (`number`), a link code (`code`, with the code), or nothing yet
  (`loading`). Signed out is also how an ended link shows.
- `LINK_BY_NUMBER` presses "Log in with phone number" (older pages say "Link with phone number"), and
  `fill_number_js(number)` puts the number in the form and presses Next. The page then shows an
  8-character code, which `STATE` reads.
- `CHATS` lists the chat list's rows, `open_chat_js(title)` opens the chat with that exact title (WhatsApp
  opens a chat only on a click, so this presses that one row and nothing else), and `MESSAGES` reads the
  messages the open chat has loaded. `SCROLL_UP` asks for older ones.
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

#: what the page shows now, as JSON: {"state": "signed_in"|"code"|"number"|"signed_out"|"loading", "code",
#: "by_number", "qr"}; "number" is the form that takes the phone number, and "qr" says the QR code has
#: loaded. Press "Log in with phone number" only after it has: pressed while the QR still loads, the
#: page stayed as it was (measured on the live page, 2026-10-25, where the QR took 13 seconds)
STATE = "(function(){" + _HELPERS + r"""
if(chatList())return JSON.stringify({state:'signed_in',code:null,by_number:false,qr:false});
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
    presses that one row and nothing else, and finds nothing for a title that is not in the list."""
    return "(function(){" + _HELPERS + r"""
var want=""" + json.dumps(title) + r""";var list=chatList();if(!list)return JSON.stringify({opened:false});
var spans=list.querySelectorAll('span[title]');for(var i=0;i<spans.length;i++){
if(spans[i].getAttribute('title')!==want)continue;var row=spans[i].closest('[role=listitem],[role=row]')||spans[i];
['mousedown','mouseup','click'].forEach(function(t){row.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));});
return JSON.stringify({opened:true});}
return JSON.stringify({opened:false});
})()"""


#: the title of the chat that is open, as JSON (a string, or null)
OPEN_TITLE = r"""(function(){var h=document.querySelector('#main header');if(!h)return JSON.stringify(null);
var t=h.querySelector('span[title]')||h.querySelector('span[dir=auto]');
return JSON.stringify(t?(t.getAttribute('title')||t.innerText||'').trim():null);})()"""

#: the messages the open chat has loaded, oldest first, as JSON: [{"id", "raw", "text", "kind"}]
MESSAGES = r"""(function(){var main=document.querySelector('#main');if(!main)return JSON.stringify(null);
var seen={};var out=[];var els=main.querySelectorAll('[data-id]');
for(var i=0;i<els.length;i++){var e=els[i];var id=e.getAttribute('data-id');if(!id||seen[id]||!/^(true|false)_/.test(id))continue;
if(e.parentElement&&e.parentElement.closest('[data-id]'))continue;seen[id]=1;
var c=e.querySelector('[data-pre-plain-text]');var raw=c?c.getAttribute('data-pre-plain-text'):null;
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

#: scroll the open chat to its top, so WhatsApp loads older messages, as JSON: {"scrolled": bool}
SCROLL_UP = r"""(function(){var main=document.querySelector('#main');if(!main)return JSON.stringify({scrolled:false});
var m=main.querySelector('[data-id]');var p=m;while(p&&p!==main){var s=getComputedStyle(p);
if(p.scrollHeight>p.clientHeight&&/(auto|scroll)/.test(s.overflowY)){p.scrollTop=0;return JSON.stringify({scrolled:true});}
p=p.parentElement;}return JSON.stringify({scrolled:false});})()"""


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
    if not isinstance(got, dict) or got.get("state") not in ("signed_in", "code", "number", "signed_out", "loading"):
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


def split_id(data_id: str) -> dict | None:
    """WhatsApp's message id on the page ("false_15550000000@c.us_3EB0A1B2C3", with the participant at
    the end in a group) as {"from_me", "chat", "message", "participant"}; None for any other form."""
    parts = (data_id or "").split("_")
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


def parse_messages(raw: str | None, *, day_first: bool = True, tz: dt.tzinfo | None = None) -> list[dict] | None:
    """`MESSAGES`'s answer as rows, oldest first: {"id", "chat", "from_me", "sender", "text", "sent_at",
    "kind", "raw"}. `id` is WhatsApp's message id and `chat` the chat's id, both from the page.
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
        rows.append({"id": ids["message"], "chat": ids["chat"], "from_me": ids["from_me"],
                     "sender": sender, "text": r.get("text") or "",
                     "sent_at": when.isoformat() if when else None,
                     "kind": r.get("kind") or "other", "raw": r.get("raw")})
    return rows
