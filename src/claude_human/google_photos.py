"""Search a Google Photos library by words and a date range, and fetch one original file, from a browser
tab signed in to photos.google.com, with no click and nothing shown on the screen.

The Google Photos web app reads the library through its own data endpoint
(`/_/PhotosUi/data/batchexecute`), with a token and a session id that the page carries in its HTML. The
scripts here call that endpoint from inside the tab, as the page does, with four read calls only:

- `lcxiM` lists the library by the date each photo was taken, newest first, from a time backwards;
- `EzkLib` is the search box (words, places, things, "receipt");
- `fDcn4b` gives one item's file name, size and time taken;
- `VrseUb` gives one item's details, among them the address of its original file.

Nothing else is called, so nothing in the library is changed, deleted, archived or shared: `rpc` in the
page refuses any call that is not in READ_CALLS. A script that runs through a browser's own automation
(`execute javascript`) runs in an isolated world, which sees the page's HTML and origin but not its
JavaScript, and that is enough here.

- `search_js(token, start_ms, end_ms, words=...)` starts a search and returns at once, and `poll_js(token)`
  says how it went: `running`, `ready` with the items (their ids, times, file names and sizes, never an
  address or a picture), or `failed` with a reason.
- `fetch_js(token, media_id)` starts fetching one original, and `chunk_js` reads it back as base64.
  `clear_js(token)` drops a result from the page.
- `search(js, start, end, words=...)` and `fetch(js, media_id)` do all of that with any function that runs
  one script in the tab and returns its string. `fetch` checks the size and the SHA-256 of the bytes it
  read back against the ones the page measured, and returns them.

The original's address is a link that works without the person's cookies, so it is kept inside the page
and never returned.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import secrets
import time

#: the only data calls the scripts make, all of them reads
READ_CALLS = ("lcxiM", "EzkLib", "fDcn4b", "VrseUb")

#: the most a fetch takes by default (a tab holds the file in memory until it is read back)
MAX_BYTES = 256 * 1024 * 1024

#: the most items a search returns, and the most result pages it reads (a page holds up to 300)
MAX_ITEMS, MAX_PAGES = 500, 20

#: the base64 a single chunk call returns (a browser's automation answers in one string)
CHUNK = 256 * 1024

#: what each failure means, for a caller to say in words
WHY = {
    "not_signed_in": "the tab shows no signed-in Google Photos page (its data token is missing)",
    "http": "Google Photos' data endpoint did not answer with 200",
    "no_data": "Google Photos' data endpoint answered without the data asked for",
    "no_item": "Google Photos holds no such item in this library",
    "no_original": "Google Photos gave no address for this item's original file",
    "too_big": "the file is larger than the most this fetch takes",
    "download": "the original file did not download",
    "size": "the downloaded file is not the size Google Photos gives for it",
    "script": "the script stopped with an error in the page",
    "timeout": "the script did not finish in time",
    "no_answer": "the tab did not run the script",
    "read_back": "the bytes read back from the tab do not match the file",
}

_ID = re.compile(r"^[0-9A-Za-z_-]{16,128}$")
_TOKEN = re.compile(r"^[0-9a-f]{8,32}$")
_MARKER = re.compile(r"__GP_[A-Z]+__")

# the page's own helpers, shared by both scripts: its token, one read call, and a failure
_COMMON = r"""var W=window.__gpRead=window.__gpRead||{};W[T]={state:'running'};
function fail(w,more){W[T]=Object.assign({state:'failed',why:w},more||{})}
function Stop(w,more){this.why=w;this.more=more}
var H=document.documentElement.innerHTML;function tok(k){var m=H.match(new RegExp('"'+k+'":"([^"]*)"'));return m?m[1]:null}
var AT=tok('SNlM0e'),SID=tok('FdrFJe'),BL=tok('cfb2h');H=null;
async function rpc(id,args){if(READ.indexOf(id)<0)throw new Error('not a read call');
 var url='/_/PhotosUi/data/batchexecute?rpcids='+id+'&source-path=%2F&f.sid='+encodeURIComponent(SID)+'&bl='+encodeURIComponent(BL)+'&hl=en&_reqid='+(Math.floor(Math.random()*90000)+10000)+'&rt=c';
 var body='f.req='+encodeURIComponent(JSON.stringify([[[id,JSON.stringify(args),null,'generic']]]))+'&at='+encodeURIComponent(AT);
 var r=await fetch(url,{method:'POST',credentials:'include',headers:{'content-type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body});
 if(r.status!==200)throw new Stop('http',{status:r.status});
 var t=await r.text(),d;t.split('\n').forEach(function(l){if(d===undefined&&l.indexOf('[["wrb.fr"')===0){var a=JSON.parse(l);if(a[0][1]===id&&a[0][2])d=JSON.parse(a[0][2])}});
 if(d===undefined)throw new Stop('no_data',{call:id});return d}
async function about(id){var e;try{e=await rpc('fDcn4b',[id,1,null,null,1])}catch(err){if(err instanceof Stop&&err.why==='no_data')return null;throw err}var x=e&&e[0];
 if(!x||x[0]!==id)return null;return {id:id,name:x[2]||null,taken_ms:x[3]||null,tz_offset_ms:x[4]||0,size:x[5]||null,width:x[6]||null,height:x[7]||null}}
function caught(e){if(e instanceof Stop)fail(e.why,e.more);else fail('script',{error:String(e&&e.message||e).slice(0,120)})}"""

_SEARCH = r"""(function(){var T=__GP_TOKEN__,WORDS=__GP_WORDS__,FROM=__GP_FROM__,TO=__GP_TO__,MAXN=__GP_MAX__,PAGES=__GP_PAGES__,READ=__GP_READ__;
""" + _COMMON + r"""
(async function(){try{
 if(!AT||!SID){fail('not_signed_in');return}
 var seen={},hits=[],page=null,pages=0,more=false,low=TO;
 while(true){
  var d=WORDS?await rpc('EzkLib',[WORDS,null,page]):await rpc('lcxiM',[page,page?null:TO,null,null,1,1]);pages++;
  var list=(d&&d[0])||[],oldest=Infinity;
  list.forEach(function(x){var t=x&&x[2];if(typeof t!=='number')return;if(t<oldest)oldest=t;
   if(t>=FROM&&t<TO&&!seen[x[0]]){seen[x[0]]=1;hits.push([x[0],t])}});
  if(oldest<low)low=oldest;
  W[T]={state:'running',pages:pages,reached_ms:WORDS?null:Math.max(low,FROM)};
  page=d&&d[1]||null;
  if(!page||(!WORDS&&oldest<FROM))break;
  if(hits.length>MAXN)break;
  if(pages>=PAGES){more=true;break}}
 var cut=hits.length>MAXN,from=null;
 if(!WORDS)from=cut?hits[MAXN-1][1]:more?Math.max(low,FROM):FROM;else if(!cut&&!more)from=FROM;
 hits=hits.slice(0,MAXN).map(function(h){return h[0]});var items=[];
 var reached=W[T].reached_ms===undefined?null:W[T].reached_ms;
 for(var i=0;i<hits.length;i+=8){W[T]={state:'running',pages:pages,reached_ms:reached,found:hits.length,read:i};
  var got=await Promise.all(hits.slice(i,i+8).map(about));got.forEach(function(g){if(g)items.push(g)})}
 items.sort(function(a,b){return (b.taken_ms||0)-(a.taken_ms||0)});
 W[T]={state:'ready',items:items,pages:pages,truncated:cut||more,complete_from_ms:from};
}catch(e){caught(e)}})();
return JSON.stringify({started:true});})()"""

_FETCH = r"""(function(){var T=__GP_TOKEN__,ID=__GP_ID__,MAX=__GP_MAX__,READ=__GP_READ__;
""" + _COMMON + r"""
(async function(){try{
 if(!AT||!SID){fail('not_signed_in');return}
 var meta=await about(ID);if(!meta){fail('no_item');return}
 if(meta.size&&meta.size>MAX){fail('too_big',{meta:meta});return}
 var info=await rpc('VrseUb',[ID,null,null,1]);var url=info&&info[1];
 if(typeof url!=='string'||url.indexOf('https://')!==0){fail('no_original',{meta:meta});return}
 var r=await fetch(url,{credentials:'include'});url=null;
 if(r.status!==200){fail('download',{meta:meta,status:r.status});return}
 meta.mimetype=(r.headers.get('content-type')||'').split(';')[0]||null;
 var data=new Uint8Array(await r.arrayBuffer());
 if(data.length>MAX){fail('too_big',{meta:meta});return}
 if(meta.size&&data.length!==meta.size){fail('size',{meta:meta,length:data.length});return}
 var h=new Uint8Array(await crypto.subtle.digest('SHA-256',data)),hex='';for(var i=0;i<h.length;i++)hex+=('0'+h[i].toString(16)).slice(-2);
 meta.sha256=hex;W[T]={state:'ready',meta:meta,length:data.length,data:data};
}catch(e){caught(e)}})();
return JSON.stringify({started:true});})()"""

_POLL = r"""(function(){var W=window.__gpRead||{};var x=W[__GP_TOKEN__];if(!x)return JSON.stringify({state:'none'});
var o={};for(var k in x){if(k!=='data')o[k]=x[k]}return JSON.stringify(o);})()"""

_CHUNK = r"""(function(){var W=window.__gpRead||{};var x=W[__GP_TOKEN__];if(!x||!x.data)return JSON.stringify(null);
var part=x.data.subarray(__GP_OFFSET__,__GP_OFFSET__+__GP_SIZE__);var s='';for(var i=0;i<part.length;i+=8192){s+=String.fromCharCode.apply(null,part.subarray(i,i+8192))}
return JSON.stringify(btoa(s));})()"""

_CLEAR = r"""(function(){var W=window.__gpRead||{};delete W[__GP_TOKEN__];return JSON.stringify({cleared:true});})()"""


def _fill(template: str, values: dict[str, str]) -> str:
    """Put each value in place of its marker in one pass, so a value is never scanned again."""
    return _MARKER.sub(lambda m: values[m.group(0)], template)


def _token(token: str) -> str:
    if not _TOKEN.match(str(token)):
        raise ValueError("a token is 8 to 32 lowercase hex characters")
    return json.dumps(token)


def _ms(when) -> int:
    """Milliseconds since the epoch, from a time with its zone or a number of milliseconds."""
    if isinstance(when, dt.datetime):
        if when.tzinfo is None:
            raise ValueError("a time needs its zone, so the day it names is not guessed")
        return int(when.timestamp() * 1000)
    return int(when)


def search_js(token: str, start, end, *, words: str | None = None, max_items: int = MAX_ITEMS,
              max_pages: int = MAX_PAGES) -> str:
    """The script that starts a search for the items taken from `start` up to (not including) `end`,
    matching `words` when given (as the search box takes them), else every item in that range."""
    start_ms, end_ms = _ms(start), _ms(end)
    if end_ms <= start_ms:
        raise ValueError("the range ends after it starts")
    if words is not None and not (0 < len(str(words).strip()) <= 200):
        raise ValueError("words are 1 to 200 characters")
    return _fill(_SEARCH, {"__GP_TOKEN__": _token(token),
                           "__GP_WORDS__": json.dumps(str(words).strip() if words else None),
                           "__GP_FROM__": str(start_ms), "__GP_TO__": str(end_ms),
                           "__GP_MAX__": str(max(1, int(max_items))), "__GP_PAGES__": str(max(1, int(max_pages))),
                           "__GP_READ__": json.dumps(list(READ_CALLS))})


def fetch_js(token: str, media_id: str, *, max_bytes: int = MAX_BYTES) -> str:
    """The script that starts fetching one item's original file in the page."""
    if not _ID.match(str(media_id)):
        raise ValueError("an item id is 16 to 128 letters, digits, '-' and '_'")
    return _fill(_FETCH, {"__GP_TOKEN__": _token(token), "__GP_ID__": json.dumps(media_id),
                          "__GP_MAX__": str(int(max_bytes)), "__GP_READ__": json.dumps(list(READ_CALLS))})


def poll_js(token: str) -> str:
    """The script that says how the work under `token` went, without a file's bytes."""
    return _fill(_POLL, {"__GP_TOKEN__": _token(token)})


def chunk_js(token: str, offset: int, size: int = CHUNK) -> str:
    """The script that returns `size` bytes of the fetched file from `offset`, as base64."""
    return _fill(_CHUNK, {"__GP_TOKEN__": _token(token), "__GP_OFFSET__": str(max(0, int(offset))),
                          "__GP_SIZE__": str(max(1, int(size)))})


def clear_js(token: str) -> str:
    """The script that drops a result from the page."""
    return _fill(_CLEAR, {"__GP_TOKEN__": _token(token)})


def _load(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def taken_at(item: dict) -> str | None:
    """When the item was taken, as an ISO 8601 time in the zone it was taken in."""
    ms = item.get("taken_ms")
    if not isinstance(ms, (int, float)):
        return None
    zone = dt.timezone(dt.timedelta(milliseconds=int(item.get("tz_offset_ms") or 0)))
    return dt.datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds")


def _at(ms, zone) -> str | None:
    """A time in milliseconds as ISO 8601 in `zone`, or None when there is no time."""
    if not isinstance(ms, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds")


def _public(item: dict) -> dict:
    return {"id": item.get("id"), "name": item.get("name"), "taken": taken_at(item),
            "size": item.get("size"), "width": item.get("width"), "height": item.get("height"),
            **({"mimetype": item["mimetype"]} if item.get("mimetype") else {})}


def _wait(js, token: str, wait_s: float, step_s: float, sleep, clock) -> dict:
    end = clock() + wait_s
    got = _load(js(poll_js(token))) or {}
    while got.get("state") in ("running", None) and clock() < end:
        sleep(step_s)
        got = _load(js(poll_js(token))) or {}
    return got


def _failed(got: dict, zone=dt.timezone.utc) -> dict:
    state = got.get("state")
    why = got.get("why") if state == "failed" and got.get("why") in WHY else "timeout"
    out = {"ok": False, "why": why, "said": WHY[why]}
    if why == "timeout" and got.get("pages"):
        # how far the search had come when the time ran out: the listing had read `pages` pages back to
        # `reached` (a date listing), and when it was reading each item's details, `read` of `found`
        # items were done. A caller can then name a range that fits rather than retry the whole one.
        out["pages"] = got["pages"]
        out["reached"] = _at(got.get("reached_ms"), zone)
        if "found" in got:
            out.update(found=got["found"], read=got.get("read"))
    if got.get("meta"):
        out["meta"] = _public(got["meta"])
    out.update({k: got[k] for k in ("status", "error", "call") if k in got})
    return out


def search(js, start, end, *, words: str | None = None, max_items: int = MAX_ITEMS,
           max_pages: int = MAX_PAGES, wait_s: float = 60.0, step_s: float = 1.0,
           sleep=time.sleep, clock=time.monotonic) -> dict:
    """The items taken from `start` up to `end` (matching `words` when given), newest first, through
    `js`, a function that runs a script in a signed-in Google Photos tab and returns its string.

    {"ok": True, "items": [{id, name, taken, size, width, height}], "pages": n, "truncated": bool,
    "complete_from": <time>} or {"ok": False, "why": <a WHY key>, "said": <its words>}. No item carries an
    address or a picture.

    `complete_from` is the time from which the items are every item up to `end`. It is `start` when
    nothing was cut. A date listing cut by `max_items` keeps the newest items, so it is the oldest one
    kept, and the older part of the range must be searched again. It is None when a word search was cut,
    because its results come in Google's order, not by date, and no part of the range is known complete.
    A timeout of a date listing carries `pages` and `reached`, the time the listing had read back to."""
    zone = start.tzinfo if isinstance(start, dt.datetime) else dt.timezone.utc
    token = secrets.token_hex(8)
    started = _load(js(search_js(token, start, end, words=words, max_items=max_items, max_pages=max_pages)))
    if not (started or {}).get("started"):
        return {"ok": False, "why": "no_answer", "said": WHY["no_answer"]}
    try:
        got = _wait(js, token, wait_s, step_s, sleep, clock)
        if got.get("state") != "ready":
            return _failed(got, zone)
        return {"ok": True, "items": [_public(i) for i in got.get("items") or []],
                "pages": got.get("pages"), "truncated": bool(got.get("truncated")),
                "complete_from": _at(got.get("complete_from_ms"), zone)}
    finally:
        js(clear_js(token))


def fetch(js, media_id: str, *, max_bytes: int = MAX_BYTES, wait_s: float = 120.0, chunk: int = CHUNK,
          step_s: float = 1.0, sleep=time.sleep, clock=time.monotonic) -> dict:
    """One item's original file through `js`.

    {"ok": True, "data": bytes, "meta": {id, name, taken, size, width, height, mimetype, sha256}} or
    {"ok": False, "why": <a WHY key>, "said": <its words>, ...}. The file is dropped from the page
    whatever happens."""
    token = secrets.token_hex(8)
    started = _load(js(fetch_js(token, media_id, max_bytes=max_bytes)))
    if not (started or {}).get("started"):
        return {"ok": False, "why": "no_answer", "said": WHY["no_answer"]}
    try:
        got = _wait(js, token, wait_s, step_s, sleep, clock)
        if got.get("state") != "ready":
            return _failed(got)
        raw = got.get("meta") or {}
        meta = {**_public(raw), "sha256": raw.get("sha256")}
        length = int(got.get("length") or 0)
        parts = []
        for offset in range(0, length, chunk):
            part = _load(js(chunk_js(token, offset, chunk)))
            if not isinstance(part, str):
                return {"ok": False, "why": "read_back", "said": WHY["read_back"], "meta": meta}
            parts.append(base64.b64decode(part))
        data = b"".join(parts)
        if len(data) != length or not meta["sha256"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            return {"ok": False, "why": "read_back", "said": WHY["read_back"], "meta": meta}
        return {"ok": True, "data": data, "meta": meta}
    finally:
        js(clear_js(token))
