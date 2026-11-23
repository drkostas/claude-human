"""Fetch the file attached to one WhatsApp Web message (a document, a picture, a video, a voice note)
from a browser tab that shows WhatsApp Web, with no click and nothing shown on the screen.

WhatsApp Web keeps each message's media details in the site's own IndexedDB (`model-storage`, store
`message`): the media key, the address of the encrypted file on WhatsApp's servers, its type, its file
name, its size and the SHA-256 of the file before and after encryption. The scripts here read that one
record, download the encrypted file, check it and decrypt it inside the page with WebCrypto, exactly as
WhatsApp Web does when a person presses Download. The media key never leaves the page.

A script that runs in the page through a browser's own automation (`brave.js_in`, AppleScript's
`execute javascript`) runs in an isolated world: it sees the page's DOM and storage, not the page's own
JavaScript. That is enough here, since IndexedDB, `fetch` and WebCrypto are the page's origin's.

- `start_js(token, chat, message_id=..., name=...)` starts the fetch and returns at once. The work goes
  on in the page, and its result waits there under `token`.
- `poll_js(token)` says how it went: `running`, `ready` (with what the file is, never its bytes), or
  `failed` with a reason. `chunk_js(token, offset, size)` returns a part of the file as base64, and
  `clear_js(token)` drops it from the page.
- `fetch(js, chat, ...)` does all of that with any function that runs one script in the tab and returns
  its string. It checks the size and the SHA-256 again on the bytes it read back, and returns them.

Which message: `chat` is the chat's id without its server (the part of "<id>@g.us" before the "@"),
and the message is named by WhatsApp's own message id, by its file name, or by a window of time it was
sent in. A message is taken only from that chat, and only when exactly one record matches its id. A
file name takes the newest message in the chat with that exact name, and says how many there were. A
time window, `sent=(since, until)` in seconds, is for a message with no file name (a photo, a voice
note): it takes the one message with a file sent in it, and when there are several it takes none and
names each one (its id, type, file name and time), so the caller can ask again by id. Every fetch says
the message's id.

Nothing here sends, opens a chat, marks a message read or presses anything.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time

#: the servers a chat's id can end in, in WhatsApp's record keys
SERVERS = ("g.us", "c.us", "lid", "s.whatsapp.net", "newsletter", "broadcast")

#: HKDF's info for each kind of media, as WhatsApp derives a file's keys from its media key
INFO = {"document": "WhatsApp Document Keys", "image": "WhatsApp Image Keys", "sticker": "WhatsApp Image Keys",
        "video": "WhatsApp Video Keys", "gif": "WhatsApp Video Keys", "ptv": "WhatsApp Video Keys",
        "audio": "WhatsApp Audio Keys", "ptt": "WhatsApp Audio Keys"}

#: the host a record's direct path is fetched from when it has no full address
MEDIA_HOST = "https://mmg.whatsapp.net"

#: the most a fetch takes by default (WhatsApp sends documents of up to 2 GB; a tab holds this one in memory)
MAX_BYTES = 64 * 1024 * 1024

#: the widest time window a message is looked for in, in seconds
SENT_SPAN_MAX = 86400

#: the base64 a single chunk call returns (a browser's automation answers in one string)
CHUNK = 256 * 1024

#: what each failure means, for a caller to say in words
WHY = {
    "no_store": "WhatsApp Web in this tab has no message store (it is not linked, or still loading)",
    "no_record": "WhatsApp Web in this tab holds no such message in that chat",
    "ambiguous": "more than one message in that chat matches, so none was taken (a time window names each one)",
    "no_media": "the message has no file this tab can fetch (no media key or no address)",
    "unknown_type": "the message's file is of a kind this does not know how to decrypt",
    "too_big": "the file is larger than the most this fetch takes",
    "http": "WhatsApp's media server did not return the file",
    "enc_hash": "the downloaded file is not the one the message names (its SHA-256 differs)",
    "mac": "the downloaded file failed its MAC check, so it was not decrypted",
    "plain_hash": "the decrypted file is not the one the message names (its SHA-256 differs)",
    "script": "the fetch stopped with an error in the page",
    "timeout": "the fetch did not finish in time",
    "no_answer": "the tab did not run the script",
    "read_back": "the bytes read back from the tab do not match the file",
}

_CHAT = re.compile(r"^[0-9A-Za-z.:-]{3,64}$")
_MESSAGE_ID = re.compile(r"^[0-9A-Za-z]{8,64}$")
_TOKEN = re.compile(r"^[0-9a-f]{8,32}$")

_START = r"""(function(){var T=__WA_TOKEN__,CHAT=__WA_CHAT__,ID=__WA_ID__,NAME=__WA_NAME__,SENT=__WA_SENT__,MAX=__WA_MAX__,SV=__WA_SERVERS__,INFO=__WA_INFO__,HOST=__WA_HOST__;
var W=window.__waMedia=window.__waMedia||{};W[T]={state:'running'};
function b64(s){var b=atob(s);var u=new Uint8Array(b.length);for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u}
function same(a,b){if(a.length!==b.length)return false;for(var i=0;i<a.length;i++)if(a[i]!==b[i])return false;return true}
function req(r){return new Promise(function(ok,no){r.onsuccess=function(){ok(r.result)};r.onerror=function(){no(r.error)}})}
function fail(w,more){W[T]=Object.assign({state:'failed',why:w},more||{})}
function each(st,range,f){return new Promise(function(ok,no){var c=st.openCursor(range);
 c.onsuccess=function(){var cur=c.result;if(!cur){ok();return}f(String(cur.key),cur.value);cur.continue()};c.onerror=function(){no(c.error)}})}
(async function(){try{
 var db=await new Promise(function(ok,no){var q=indexedDB.open('model-storage');var fresh=false;
  q.onupgradeneeded=function(){fresh=true;q.transaction.abort()};q.onsuccess=function(){ok(q.result)};q.onerror=function(){no(fresh?null:q.error)}}).catch(function(){return null});
 if(!db||!db.objectStoreNames.contains('message')){if(db)db.close();fail('no_store');return}
 function st(){return db.transaction('message','readonly').objectStore('message')}var hits=[];
 for(var f=0;f<2;f++){for(var s=0;s<SV.length;s++){var pre=(f?'true':'false')+'_'+CHAT+'@'+SV[s]+'_';
  if(ID){var exact=await req(st().get(pre+ID));if(exact)hits.push({k:pre+ID,v:exact});
   await each(st(),IDBKeyRange.bound(pre+ID+'_',pre+ID+'_￿'),function(k,v){hits.push({k:k,v:v})})}
  else if(SENT){await each(st(),IDBKeyRange.bound(pre,pre+'￿'),function(k,v){
   if(v&&INFO[v.type]&&v.t>=SENT[0]&&v.t<SENT[1])hits.push({k:k,v:v})})}
  else{await each(st(),IDBKeyRange.bound(pre,pre+'￿'),function(k,v){if(v&&v.filename===NAME)hits.push({k:k,v:v})})}}}
 db.close();
 function mid(k){var p=String(k).split('_');return p.length>2?p[2]:null}
 if(!hits.length){fail('no_record');return}
 if(ID&&hits.length>1){fail('ambiguous',{matches:hits.length});return}
 if(SENT&&hits.length>1){hits.sort(function(a,b){return (a.v.t||0)-(b.v.t||0)});
  fail('ambiguous',{matches:hits.length,candidates:hits.slice(0,10).map(function(h){
   return {id:mid(h.k),type:h.v.type||null,filename:h.v.filename||null,sent_at:h.v.t||null}})});return}
 hits.sort(function(a,b){return (b.v.t||0)-(a.v.t||0)});var rec=hits[0].v;
 var meta={id:mid(hits[0].k),type:rec.type||null,mimetype:rec.mimetype||null,filename:rec.filename||null,size:rec.size||null,
  sent_at:rec.t||null,filehash:rec.filehash||null,matches:hits.length};
 if(!rec.mediaKey||!(rec.deprecatedMms3Url||rec.directPath)||!rec.filehash){fail('no_media',{meta:meta});return}
 if(!INFO[rec.type]){fail('unknown_type',{meta:meta});return}
 if(rec.size&&rec.size>MAX){fail('too_big',{meta:meta});return}
 var r=await fetch(rec.deprecatedMms3Url||(HOST+rec.directPath));
 if(r.status!==200){fail('http',{meta:meta,status:r.status});return}
 var enc=new Uint8Array(await r.arrayBuffer());
 if(enc.length>MAX+10||enc.length<26){fail(enc.length>MAX?'too_big':'enc_hash',{meta:meta});return}
 if(rec.encFilehash&&!same(new Uint8Array(await crypto.subtle.digest('SHA-256',enc)),b64(rec.encFilehash))){fail('enc_hash',{meta:meta});return}
 var base=await crypto.subtle.importKey('raw',b64(rec.mediaKey),'HKDF',false,['deriveBits']);
 var k=new Uint8Array(await crypto.subtle.deriveBits({name:'HKDF',hash:'SHA-256',salt:new Uint8Array(32),info:new TextEncoder().encode(INFO[rec.type])},base,112*8));
 var iv=k.slice(0,16),ck=k.slice(16,48),mk=k.slice(48,80),body=enc.slice(0,enc.length-10),mac=enc.slice(enc.length-10);
 var signed=new Uint8Array(iv.length+body.length);signed.set(iv,0);signed.set(body,iv.length);
 var hk=await crypto.subtle.importKey('raw',mk,{name:'HMAC',hash:'SHA-256'},false,['sign']);
 if(!same(new Uint8Array(await crypto.subtle.sign('HMAC',hk,signed)).slice(0,10),mac)){fail('mac',{meta:meta});return}
 var ak=await crypto.subtle.importKey('raw',ck,'AES-CBC',false,['decrypt']);
 var plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-CBC',iv:iv},ak,body));
 if(!same(new Uint8Array(await crypto.subtle.digest('SHA-256',plain)),b64(rec.filehash))){fail('plain_hash',{meta:meta});return}
 W[T]={state:'ready',meta:meta,length:plain.length,data:plain};
}catch(e){fail('script',{error:String(e&&e.name||e).slice(0,80)})}})();
return JSON.stringify({started:true});})()"""

POLL = r"""(function(){var W=window.__waMedia||{};var x=W[__WA_TOKEN__];if(!x)return JSON.stringify({state:'none'});
var o={};for(var k in x){if(k!=='data')o[k]=x[k]}return JSON.stringify(o);})()"""

CHUNK_JS = r"""(function(){var W=window.__waMedia||{};var x=W[__WA_TOKEN__];if(!x||!x.data)return JSON.stringify(null);
var part=x.data.subarray(__WA_OFFSET__,__WA_OFFSET__+__WA_SIZE__);var s='';for(var i=0;i<part.length;i+=8192){s+=String.fromCharCode.apply(null,part.subarray(i,i+8192))}
return JSON.stringify(btoa(s));})()"""

CLEAR = r"""(function(){var W=window.__waMedia||{};delete W[__WA_TOKEN__];return JSON.stringify({cleared:true});})()"""


_MARKER = re.compile(r"__WA_[A-Z]+__")


def _fill(template: str, values: dict[str, str]) -> str:
    """Put each value in place of its marker in one pass, so a value is never scanned again: a file
    name that holds another marker's word stays as it is. A marker with no value is an error."""
    return _MARKER.sub(lambda m: values[m.group(0)], template)


def _token(token: str) -> str:
    if not _TOKEN.match(str(token)):
        raise ValueError("a token is 8 to 32 lowercase hex characters")
    return json.dumps(token)


def _sent(sent) -> list[int] | None:
    """A time window as [since, until) in whole seconds, or None. At most a day wide."""
    if sent is None:
        return None
    if not isinstance(sent, (tuple, list)) or len(sent) != 2 \
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in sent):
        raise ValueError("a time window is (since, until) in whole seconds")
    since, until = sent
    if since < 0 or until <= since or until - since > SENT_SPAN_MAX:
        raise ValueError(f"a time window starts at 0 or later, ends after it starts and spans at most "
                         f"{SENT_SPAN_MAX} seconds")
    return [since, until]


def start_js(token: str, chat: str, *, message_id: str | None = None, name: str | None = None,
             sent: tuple[int, int] | None = None, max_bytes: int = MAX_BYTES) -> str:
    """The script that starts fetching one message's file in the page. Exactly one of `message_id`,
    `name` and `sent` names the message. Every value goes in as JSON, and a chat or message id that is
    not one is refused here, before any script runs."""
    if not _CHAT.match(str(chat)) or "@" in str(chat):
        raise ValueError("a chat is its id without the server, the part before the @")
    if sum(x is not None for x in (message_id, name, sent)) != 1:
        raise ValueError("name the message one way: by its id, its file name or the time it was sent")
    window = _sent(sent)
    if message_id is not None and not _MESSAGE_ID.match(str(message_id)):
        raise ValueError("a message id is 8 to 64 letters and digits")
    if name is not None and not (0 < len(str(name)) <= 255):
        raise ValueError("a file name is 1 to 255 characters")
    return _fill(_START, {"__WA_TOKEN__": _token(token), "__WA_CHAT__": json.dumps(chat),
                          "__WA_ID__": json.dumps(message_id), "__WA_NAME__": json.dumps(name),
                          "__WA_SENT__": json.dumps(window),
                          "__WA_MAX__": str(int(max_bytes)), "__WA_SERVERS__": json.dumps(list(SERVERS)),
                          "__WA_INFO__": json.dumps(INFO), "__WA_HOST__": json.dumps(MEDIA_HOST)})


def poll_js(token: str) -> str:
    """The script that says how the fetch under `token` went, without the file's bytes."""
    return _fill(POLL, {"__WA_TOKEN__": _token(token)})


def chunk_js(token: str, offset: int, size: int = CHUNK) -> str:
    """The script that returns `size` bytes of the fetched file from `offset`, as base64."""
    return _fill(CHUNK_JS, {"__WA_TOKEN__": _token(token), "__WA_OFFSET__": str(max(0, int(offset))),
                            "__WA_SIZE__": str(max(1, int(size)))})


def clear_js(token: str) -> str:
    """The script that drops the fetched file from the page."""
    return _fill(CLEAR, {"__WA_TOKEN__": _token(token)})


def _load(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def safe_name(name: str | None, fallback: str = "attachment") -> str:
    """A file name that is safe to write in one folder: no folder parts, no control characters, no
    leading dot, at most 120 characters with its extension kept."""
    text = "".join(c for c in str(name or "") if c.isprintable() and c not in "/\\:")
    text = text.strip().lstrip(".").strip() or fallback
    if len(text) > 120:
        stem, dot, ext = text.rpartition(".")
        text = (stem[: 120 - len(ext) - 1] + "." + ext) if dot and 0 < len(ext) <= 10 else text[:120]
    return text


def fetch(js, chat: str, *, message_id: str | None = None, name: str | None = None,
          sent: tuple[int, int] | None = None, max_bytes: int = MAX_BYTES, wait_s: float = 120.0, chunk: int = CHUNK, step_s: float = 1.0,
          sleep=time.sleep, clock=time.monotonic) -> dict:
    """Fetch one message's file through `js`, a function that runs a script in a WhatsApp Web tab and
    returns its string (None when the tab did not answer).

    {"ok": True, "data": bytes, "meta": {...}} or {"ok": False, "why": <a WHY key>, "said": <its words>,
    ...}. `meta` holds the message's id, type, mimetype, file name, size, the time it was sent (seconds) and
    how many messages matched; never the media key. An `ambiguous` time window also gives `candidates`. The file is dropped from the page whatever happens."""
    token = secrets.token_hex(8)
    started = _load(js(start_js(token, chat, message_id=message_id, name=name, sent=sent, max_bytes=max_bytes)))
    if not (started or {}).get("started"):
        return {"ok": False, "why": "no_answer", "said": WHY["no_answer"]}
    try:
        end = clock() + wait_s
        got = _load(js(poll_js(token))) or {}
        while got.get("state") in ("running", None) and clock() < end:
            sleep(step_s)
            got = _load(js(poll_js(token))) or {}
        state = got.get("state")
        if state != "ready":
            why = got.get("why") if state == "failed" and got.get("why") in WHY else "timeout"
            out = {"ok": False, "why": why, "said": WHY[why]}
            out.update({k: got[k] for k in ("meta", "matches", "candidates", "status", "error") if k in got})
            return out
        meta = {k: v for k, v in (got.get("meta") or {}).items() if k != "filehash"}
        length = int(got.get("length") or 0)
        parts = []
        for offset in range(0, length, chunk):
            part = _load(js(chunk_js(token, offset, chunk)))
            if not isinstance(part, str):
                return {"ok": False, "why": "read_back", "said": WHY["read_back"], "meta": meta}
            parts.append(base64.b64decode(part))
        data = b"".join(parts)
        want = (got.get("meta") or {}).get("filehash")
        if len(data) != length or (meta.get("size") and len(data) != meta["size"]) or not want \
                or hashlib.sha256(data).digest() != base64.b64decode(want):
            return {"ok": False, "why": "read_back", "said": WHY["read_back"], "meta": meta}
        return {"ok": True, "data": data, "meta": meta}
    finally:
        js(clear_js(token))
