"""The page a person opens on their phone: a live picture of one window, and a trackpad for it.

The page is static and carries no secret. Whatever opened it hands the token in afterwards (a
WebView calls the grant function, a parent window posts a message), and every request the page then
makes sends the token as a bearer header. The token never goes into a URL. With ``ask_token`` on,
a page opened directly in a browser asks for the token in a password field instead.

The gestures live here, not in the app that shows the page. A phone driving a desktop is a trackpad,
not a touchscreen: one finger moves a pointer drawn on the page, a tap clicks, a long press is a
right click, two fingers scroll or pinch, a double tap held down drags. So any browser can use the
same page.
"""
from __future__ import annotations

import json

DEFAULTS = {
    "title": "Mac station",
    "grant_fn": "__stationGrant",
    "pending_var": "__stationPendingGrant",
    "message_type": "station:grant",
    "guide_key": "station_guide_v2",
    "name": "The station",
    "offline": "Lost the connection to the Mac.",
    "waiting": "Waiting for the token for this window.",
}

_IDENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def render(*, ask_token: bool = True, **names: str) -> str:
    """The page, with the JavaScript names and texts the embedding app uses.

    ``grant_fn`` and ``pending_var`` are global names the app calls or sets, ``message_type`` is the
    postMessage type it sends, and the rest are texts shown on the page."""
    unknown = set(names) - set(DEFAULTS)
    if unknown:
        raise TypeError(f"unknown view names: {sorted(unknown)}")
    v = {**DEFAULTS, **names}
    for key in ("grant_fn", "pending_var"):
        if not v[key] or not set(v[key]) <= _IDENT or v[key][0].isdigit():
            raise ValueError(f"{key} must be a JavaScript identifier, not {v[key]!r}")
    html = _TEMPLATE
    for key, value in v.items():
        if key in ("grant_fn", "pending_var"):
            text = value
        elif key == "title":
            text = value.replace("&", "&amp;").replace("<", "&lt;")
        else:
            # inside a single-quoted JavaScript string
            text = json.dumps(value)[1:-1].replace("'", "\\'")
        html = html.replace("@@" + key.upper() + "@@", text)
    html = html.replace("@@ASK_TOKEN@@", "true" if ask_token else "false")
    assert "@@" not in html
    return html


_TEMPLATE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>@@TITLE@@</title><style>
 html,body{margin:0;height:100%;background:#0a1720;overflow:hidden;
   -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
 #wrap{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
   touch-action:none}
 img{max-width:100%;max-height:100%;touch-action:none;display:block;pointer-events:none}
 #kb{position:fixed;opacity:0;height:1px;width:1px;border:0;top:0;left:0}
 /* the stream does not carry the Mac's cursor, so the pointer is drawn here. Without
    it a trackpad is unusable: you cannot aim at something you cannot see. */
 #dot{position:fixed;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;
   border:2px solid #7fe3ef;background:rgba(127,227,239,.28);pointer-events:none;
   z-index:8;transition:none}
 #dot.press{background:rgba(127,227,239,.85)}
 #bar{position:fixed;left:0;right:0;bottom:0;display:flex;gap:8px;padding:8px;
   background:rgba(10,23,32,.92);font:13px -apple-system,system-ui,sans-serif}
 button{flex:1;padding:10px;border:0;border-radius:8px;background:#152232;color:#e6f0f5;
   font:inherit}
 button:active{background:#1e3140}
 #hint{position:fixed;top:0;left:0;right:0;padding:6px 10px;color:#5a7a8a;
   font:12px -apple-system,system-ui,sans-serif;background:rgba(10,23,32,.85)}
 #err{display:none;position:fixed;top:28px;left:0;right:0;padding:10px;z-index:9;
   background:#e06060;color:#0a1720;font:600 13px -apple-system,system-ui,sans-serif}
 /* ⚠️ ALWAYS VISIBLE. "It does not work" is not something anyone can act on, and this thing
    has three places to break: the touch never arrives, the request never lands, or the Mac
    refuses it. The line says which. */
 #diag{position:fixed;bottom:64px;left:0;right:0;padding:5px 10px;z-index:9;color:#77c8d1;
   font:11px ui-monospace,Menlo,monospace;background:rgba(10,23,32,.9)}
 #help{position:fixed;top:4px;right:6px;z-index:12;width:30px;height:30px;border-radius:50%;
   background:rgba(21,34,50,.92);color:#77c8d1;border:0;font:600 16px system-ui;padding:0}
 #guide{display:none;position:fixed;inset:0;z-index:20;background:rgba(10,23,32,.97);
   padding:22px 20px;overflow:auto;font:15px -apple-system,system-ui,sans-serif;color:#e6f0f5}
 #guide h2{font-size:19px;margin:4px 0 18px}
 #guide .g{display:flex;gap:12px;margin-bottom:15px;line-height:1.35}
 #guide .g b{color:#77c8d1;flex:0 0 104px}
 #guide .g span{flex:1;color:#cfe0e6}
 #guide #guideok{margin-top:10px;width:100%;padding:13px;border:0;border-radius:10px;
   background:#77c8d1;color:#0a1720;font:600 15px system-ui}
 #scroll{position:fixed;right:8px;top:50%;transform:translateY(-50%);z-index:11;
   display:flex;flex-direction:column;gap:7px}
 #scroll button{width:48px;height:48px;border-radius:11px;background:rgba(21,34,50,.9);
   color:#e6f0f5;border:0;font:600 20px system-ui;padding:0;line-height:1}
 #scroll button:active{background:#274058}

 /* ⚠️ LANDSCAPE IS NOT PORTRAIT MADE WIDER. The bottom strip costs the vertical space a
    landscape phone has least of, and the picture is the whole point of the page. So the strip
    becomes a column on the RIGHT, where the holding hand already is.

    ⛔ AND THE SCROLL ARROWS HAVE TO MOVE, which is the part that is easy to miss: #scroll is
    already pinned right, so a right-hand control column lands straight on top of it. One
    combined column does not fit either — six controls plus four arrows at 48px is 480px against
    a ~390px landscape height. Arrows left, controls right, picture between them, and every
    fixed element inset from BOTH columns so nothing ends up under a button. */
 @media (orientation:landscape){
   #bar{left:auto;top:0;bottom:0;right:0;width:98px;flex-direction:column;
     justify-content:center;gap:6px;padding:8px}
   #bar button{flex:0 0 auto;padding:11px 6px}
   #scroll{right:auto;left:8px}
   #wrap{left:66px;right:98px}
   #hint{right:98px}
   #err{right:98px}
   #help{right:106px}
   #diag{bottom:0;left:66px;right:98px}
 }
</style></head><body>
<div id=hint>One finger moves the pointer · tap = click · pinch = zoom · two fingers = scroll</div>
<button id=help aria-label="How to use">?</button>
<div id=guide>
 <h2>Using the Mac window</h2>
 <div class=g><b>Move</b><span>Drag one finger. The dot shows where the pointer is.</span></div>
 <div class=g><b>Click</b><span>Tap once, or the Click button below.</span></div>
 <div class=g><b>Right-click</b><span>Press and hold, or the Right button.</span></div>
 <div class=g><b>Zoom</b><span>Pinch two fingers to zoom in and out.</span></div>
 <div class=g><b>Scroll</b><span>Drag two fingers. When zoomed in, two fingers pan the view.</span></div>
 <div class=g><b>Grab &amp; drag</b><span>Double-tap, then keep the finger down and drag — for scrollbars, sliders, icons.</span></div>
 <div class=g><b>Type</b><span>The Keyboard button opens your phone's keyboard.</span></div>
 <button id=guideok>Got it</button>
</div>
<div id=err></div>
<form id=tokform style="display:none;position:fixed;top:72px;left:16px;right:16px;z-index:21;gap:8px"><input id=tok type=password autocomplete=off placeholder="Station token" style="flex:1;padding:10px;border:0;border-radius:8px;font:15px system-ui"><button type=submit style="flex:0 0 auto">Open</button></form>
<div id=diag>touches 0 · nothing sent yet</div>
<div id=wrap><img id=v alt=""><div id=dot></div></div>
<div id=scroll>
 <button id=stop aria-label="Jump to top">⤒</button>
 <button id=sup aria-label="Scroll up">▲</button>
 <button id=sdn aria-label="Scroll down">▼</button>
 <button id=sbot aria-label="Jump to bottom">⤓</button>
</div>
<input id=kb autocapitalize=off autocorrect=off autocomplete=off spellcheck=false>
<div id=bar><button id=lclick>Click</button><button id=rclick>Right</button>
<button id=keyb>Keyboard</button><button id=esc>Esc</button>
<button id=ret>Return</button><button id=tab>Tab</button><button id=spot>Spotlight</button></div>
<script>
(function(){
 var qs=new URLSearchParams(location.search);
 var app=qs.get('app')||'', win=qs.get('window')||'';
 var img=document.getElementById('v');
 // gestures are taken on the WRAP, not the picture: the picture is letterboxed, and a tap on the
 // dark margin used to land on nothing and look like the whole thing was dead.
 var surf=document.getElementById('wrap');
 // ⚠️ THE GRANT IS NOT IN THE URL. This page is static and unauthenticated;
 // the app that opened it hands the grant in afterwards — a WebView through window.@@GRANT_FN@@,
 // a browser through postMessage — and only then does the picture start. Every request the page
 // makes carries the grant as a Bearer header, so nothing here lands in a history, a log or a
 // share sheet.
 var grant=null;
 function auth(){ return {'Authorization':'Bearer '+grant}; }
 window.@@GRANT_FN@@=function(g){ if(!g||g===grant) return; grant=g; ok(); startStream(); };
 window.addEventListener('message',function(e){
   // ⛔ ONLY THE WINDOW THAT EMBEDS OR OPENED THIS PAGE MAY HAND IT A GRANT. /view is a static
   // page anyone may load, and its entire security property is that the grant reaches it from the
   // app and nowhere else. This listener accepted a grant from ANY window with a handle to it, so
   // a hostile embedder could drive what this page connects to. It cannot READ the stream back
   // across origins, so this was never a way to steal a picture — it is the one missing line of
   // defence in the page's only trust boundary, and it costs nothing to add.
   if(e.source!==window.parent && e.source!==window.opener) return;
   if(e.data&&e.data.type==='@@MESSAGE_TYPE@@') window.@@GRANT_FN@@(e.data.grant);
 });
 // the stream is read by hand rather than by an <img src>: an <img> cannot send a header, and
 // a header is the one place the grant is allowed to be.
 // ⚠️ A BROWSER WITH NO APP IN FRONT OF IT ASKS FOR THE TOKEN ON THE PAGE. It goes into a password
 // field and from there into a header, never into the address, so it still never rides a URL.
 function waitForGrant(){
   fail('@@WAITING@@');
   if(!@@ASK_TOKEN@@) return;
   var f=document.getElementById('tokform'); f.style.display='flex';
   f.onsubmit=function(e){ e.preventDefault(); var v=document.getElementById('tok').value.trim();
     if(v){ f.style.display='none'; window.@@GRANT_FN@@(v); } };
 }
 var streaming=false;
 function startStream(){
   if(!grant||streaming) return;
   streaming=true;
   var url='/stream?'+(app?('app='+encodeURIComponent(app)+'&'):'')+(win?('window='+win+'&'):'')+'fps=8';
   fetch(url,{headers:auth()}).then(function(r){
     if(r.status===401){ streaming=false; fail('This session expired. Go back and open the task again.'); return; }
     if(!r.ok||!r.body){ streaming=false; fail('The picture could not start (HTTP '+r.status+').'); return; }
     var reader=r.body.getReader(), buf=new Uint8Array(0), prev=null;
     var CRLF2=new Uint8Array([13,10,13,10]), dec=new TextDecoder();
     function concat(a,b){ var c=new Uint8Array(a.length+b.length); c.set(a,0); c.set(b,a.length); return c; }
     function indexOf(hay,needle){
       outer: for(var i=0;i<=hay.length-needle.length;i++){
         for(var j=0;j<needle.length;j++){ if(hay[i+j]!==needle[j]) continue outer; }
         return i;
       }
       return -1;
     }
     function pump(){
       reader.read().then(function(res){
         if(res.done){ streaming=false; fail('The picture stopped. If this session is old, reopen the task.'); return; }
         buf=concat(buf,res.value);
         for(;;){
           var h=indexOf(buf,CRLF2); if(h<0) break;
           var m=/Content-Length:\\s*(\\d+)/i.exec(dec.decode(buf.subarray(0,h)));
           if(!m){ buf=buf.subarray(h+4); continue; }
           var n=parseInt(m[1],10), start=h+4;
           if(buf.length<start+n) break;
           var u=URL.createObjectURL(new Blob([buf.subarray(start,start+n)],{type:'image/jpeg'}));
           img.src=u; if(prev) URL.revokeObjectURL(prev); prev=u;
           buf=buf.subarray(start+n);
         }
         if(buf.byteOffset>(1<<20)) buf=new Uint8Array(buf);
         pump();
       }).catch(function(){ streaming=false; fail('@@OFFLINE@@'); });
     }
     pump(); ok();
   }).catch(function(){ streaming=false; fail('@@OFFLINE@@'); });
 }
 // a grant handed over before this script ran is waiting on the window
 if(window.@@PENDING_VAR@@) window.@@GRANT_FN@@(window.@@PENDING_VAR@@);
 else waitForGrant();
 // ⚠️ FALL BACK TO THE WHOLE SURFACE. If the picture has not sized yet — a slow stream, a
 // refused one — its rect is empty, every touch is judged "outside" and the screen is dead with
 // no explanation. A surface that cannot say where the picture is should still accept a touch.
 function picRect(){
   var r=img.getBoundingClientRect();
   if(r.width>10&&r.height>10) return r;
   return surf.getBoundingClientRect();
 }
 function norm(t){ // where the touch is INSIDE the picture, 0..1 — the phone never sends pixels
   var r=picRect();
   return {x:Math.min(1,Math.max(0,(t.clientX-r.left)/r.width)),
           y:Math.min(1,Math.max(0,(t.clientY-r.top)/r.height))};
 }
 // the surface is bigger than the picture; a touch on the margin must be ignored rather than
 // clamped, or the edge of the remote window gets clicked by someone aiming at nothing.
 function inside(t){
   var r=picRect();
   return t.clientX>=r.left&&t.clientX<=r.right&&t.clientY>=r.top&&t.clientY<=r.bottom;
 }
 // ⚠️ A GESTURE THAT FAILS SILENTLY IS THE WORST FAILURE THIS THING HAS. It looks exactly like
 // a dead app: taps do nothing and nothing says why. This used to end in .catch(function(){}),
 // so an expired grant — the ordinary case, after half an hour on one task — made every gesture
 // vanish. Whatever goes wrong now says so on the glass.
 var touchCount=0;
 function diag(what){
   document.getElementById('diag').textContent =
     'touches '+touchCount+' · '+what+' · pic '+Math.round(picRect().width)+'x'+Math.round(picRect().height);
 }
 function fail(msg){
   var b=document.getElementById('err');
   b.textContent=msg; b.style.display='block';
 }
 function ok(){ document.getElementById('err').style.display='none'; }
 function send(m){
   m.app=app; if(win) m.window=win;
   if(!grant){ diag(m.type+' → no grant yet'); return; }
   var h=auth(); h['Content-Type']='application/json';
   fetch('/input',{method:'POST',headers:h,body:JSON.stringify(m)})
     .then(function(r){
       diag(m.type+' → '+r.status);
       if(r.status===200) r.clone().json().then(function(j){
         if(j && j.blocked) fail(j.detail);
       }).catch(function(){});
       if(r.status===401) fail('This session expired. Go back and open the task again.');
       else if(!r.ok) fail('@@NAME@@ could not act on that (HTTP '+r.status+').');
       else ok();
     })
     .catch(function(e){ diag(m.type+' → no reply');
       fail('@@OFFLINE@@'); });
 }
 // ⚠️ A PHONE IS A TRACKPAD FOR THE MAC, NOT A TOUCHSCREEN ONTO IT.
 // Mapping the finger straight onto the desktop reads well and cannot be used: a fingertip
 // covers something like forty real pixels, so a checkbox is not reachable, and there is no way
 // to see where you are BEFORE committing to a click. It also turns every accidental brush into
 // a click somewhere. So the finger MOVES a pointer, exactly as a laptop trackpad does, and the
 // click is a separate deliberate act — a tap, or one of the two buttons.
 //
 // The pointer is drawn here because the stream does not carry the Mac's cursor. Aiming at
 // something invisible is the original problem restated.
 var dot=document.getElementById('dot');
 var cur={x:0.5,y:0.5};          // where the pointer is, as a fraction of the picture
 var SENS=1.7;                   // finger travel -> pointer travel
 function drawCur(){
   var r=picRect();
   dot.style.left=(r.left+cur.x*r.width)+'px';
   dot.style.top =(r.top +cur.y*r.height)+'px';
 }
 // ⚠️ A DEGENERATE RECT POISONS THE POINTER WITH NaN, AND NaN SERIALISES AS null.
 // If the picture has not sized yet, r.width is 0, cur.x becomes NaN, and every later message
 // carries null coordinates — the Mac answers HTTP 400 and the surface is dead from then on,
 // because NaN never recovers. So the step is dropped rather than allowed to poison cur.
 function moveBy(dx,dy){
   var r=picRect();
   if(!(r.width>0&&r.height>0)) return;
   var nx=cur.x+dx*SENS/r.width, ny=cur.y+dy*SENS/r.height;
   if(!isFinite(nx)||!isFinite(ny)) return;
   cur.x=Math.min(1,Math.max(0,nx));
   cur.y=Math.min(1,Math.max(0,ny));
   drawCur();
   send({type:'move',x:cur.x,y:cur.y});
 }
 function press(kind,button){
   if(!isFinite(cur.x)||!isFinite(cur.y)){ cur={x:0.5,y:0.5}; drawCur(); }
   dot.classList.add('press');
   setTimeout(function(){dot.classList.remove('press');},160);
   if(navigator.vibrate) navigator.vibrate(10);
   var m={type:kind,x:cur.x,y:cur.y};
   if(button) m.button=button;
   send(m);
 }
 window.addEventListener('resize',drawCur);
 img.addEventListener('load',drawCur);
 setTimeout(drawCur,300);

 var lastRaw=null, moved=false, holdTimer=null, lastTap=0;
 var twoFinger=false, lastC=null, dragMode=false, lastDist=0, zoom=1, panX=0, panY=0;
 var g2mode=null, g2startDist=0, g2startC=null;
 function centroid(ts){var x=0,y=0;for(var i=0;i<ts.length;i++){x+=ts[i].clientX;y+=ts[i].clientY;}
   return {x:x/ts.length,y:y/ts.length};}
 function spread(ts){var dx=ts[0].clientX-ts[1].clientX,dy=ts[0].clientY-ts[1].clientY;return Math.hypot(dx,dy);}
 function applyView(){
   if(zoom<=1.001){zoom=1;panX=0;panY=0;}
   var vw=surf.clientWidth||1, vh=surf.clientHeight||1;
   // ⚠️ THE SLACK IS THE PICTURE'S, NOT THE SURFACE'S. The surface fills the phone and the
   // picture is letterboxed inside it — a 16:10 Mac screen on a portrait phone leaves most of the
   // height empty — so slack measured from the surface lets the picture be dragged clean off the
   // top. Pan as far as the picture overhangs and no further; when it is still smaller than the
   // surface there is nothing to pan to, so it stays centred.
   // A picture that has not sized yet falls back to the surface, which is the old expression
   // exactly, rather than clamping to nought and freezing the gesture on a slow stream.
   var pw=img.offsetWidth||vw, ph=img.offsetHeight||vh;
   var slackX=Math.max(0,(pw*zoom-vw)/2), slackY=Math.max(0,(ph*zoom-vh)/2);
   panX=Math.min(slackX,Math.max(-slackX,panX));
   panY=Math.min(slackY,Math.max(-slackY,panY));
   img.style.transformOrigin='center center';
   img.style.transform='translate('+panX+'px,'+panY+'px) scale('+zoom+')';
   drawCur();
 }

 surf.addEventListener('touchstart',function(e){
   e.preventDefault();
   touchCount++;
   if(e.touches.length===2){twoFinger=true;lastC=centroid(e.touches);lastDist=spread(e.touches);
     g2startC=lastC; g2startDist=lastDist; g2mode=null;
     clearTimeout(holdTimer); diag('two fingers — scroll or pinch'); return;}
   twoFinger=false; moved=false;
   lastRaw={x:e.touches[0].clientX,y:e.touches[0].clientY};
   // a second tap that stays down holds the button while you move: a real drag, for sliders and
   // text selection, which is how a trackpad does it too.
   dragMode = (Date.now()-lastTap) < 320;
   if(dragMode){ send({type:'down',x:cur.x,y:cur.y}); dot.classList.add('press'); diag('drag'); }
   else {
     holdTimer=setTimeout(function(){ if(!moved){ press('click','right'); lastRaw=null; } }, 550);
   }
 },{passive:false});

 surf.addEventListener('touchmove',function(e){
   e.preventDefault();
   if(twoFinger&&e.touches.length===2){
     var c=centroid(e.touches), d=spread(e.touches);
     var dDist=d-g2startDist, dPan=Math.hypot(c.x-g2startC.x,c.y-g2startC.y);
     // ⚠️ DECIDE ONCE, THEN COMMIT. A two-finger scroll always jitters the finger distance a
     // little, so zooming on any distance change made scrolling impossible. Wait until one signal
     // clearly wins (past a dead-zone, and clearly bigger than the other), lock that mode for the
     // whole gesture, and never do the other until the fingers lift.
     if(g2mode===null){
       // ⚠️ ONCE ZOOMED IN, TWO FINGERS USUALLY MEAN "MOVE ME", NOT "ZOOM FURTHER". Someone who
       // has already zoomed is looking for the part they cannot see, so a pinch has to be much
       // more convincing before it beats a pan.
       var zFloor = zoom>1.03 ? 30 : 16, zBias = zoom>1.03 ? 2.2 : 1.4;
       if(Math.abs(dDist)>zFloor && Math.abs(dDist)>dPan*zBias) g2mode='zoom';
       else if(dPan>16 && dPan>Math.abs(dDist)*1.4) g2mode='scroll';
     } else if(g2mode==='zoom' && zoom>1.03){
       // ⛔ AND THE LOCK IS NOT PERMANENT ANY MORE — THAT WAS THE PAN BUG. Two fingers dragged
       // across a phone splay as they travel, so a pan that opened with a bit over 16px of
       // spread-jitter latched 'zoom' for the WHOLE gesture: it zoomed a hair and then ignored
       // every further movement, because the pan branch was unreachable until the fingers lifted.
       // Reported exactly as "nothing moves, mistakenly zooms sometimes", which is one bug and
       // not two. So while zoomed, zoom mode is escapable: if the fingers are travelling together
       // and are no longer spreading, the person is panning, and the gesture says so every frame.
       var recentPan=Math.hypot(c.x-lastC.x,c.y-lastC.y), recentSpread=Math.abs(d-lastDist);
       if(recentPan>3 && recentPan>recentSpread*2) g2mode='scroll';
     }
     if(g2mode==='zoom'){
       // ⭐ ZOOM ABOUT THE FINGERS, NOT THE MIDDLE OF THE SCREEN. transformOrigin is fixed at the
       // centre, so scaling alone always pulls the picture toward the middle: you pinch on the
       // thing you want a closer look at and it slides away from you. Keeping the point under the
       // fingers still is what makes a pinch feel like a pinch, and it is the whole reason to
       // pinch THERE rather than anywhere.
       //
       // With `translate(pan) scale(z)` about the centre, a point d away from the centre sits at
       // pan + z*d_element. Holding the focal point still across z0 -> z1 means:
       //     pan1 = d - (z1/z0) * (d - pan0)
       if(lastDist>0){
         var z0=zoom, z1=Math.min(4,Math.max(1,zoom*(d/lastDist)));
         if(z1!==z0){
           // ⚠️ THE ORIGIN COMES FROM THE PICTURE, NOT THE WRAPPER. The surface is bigger than
           // the picture (see picRect) and need not sit at the window's corner, so measuring the
           // centre as half the wrapper would zoom about a point the picture is not centred on
           // and drift a little further off with every pinch. Scaling about the centre does not
           // move the centre, so the untransformed centre is the current one minus the pan.
           var pr=img.getBoundingClientRect(), k=z1/z0;
           if(pr.width>10&&pr.height>10){
             var fx=c.x-(pr.left+pr.width/2-panX), fy=c.y-(pr.top+pr.height/2-panY);
             panX=fx-k*(fx-panX); panY=fy-k*(fy-panY);
           }
           zoom=z1;
         }
       }
       applyView(); diag('zoom '+zoom.toFixed(2)+'x');
     } else if(g2mode==='scroll'){
       var mx=c.x-lastC.x, my=c.y-lastC.y;
       if(zoom>1.03){ panX+=mx; panY+=my; applyView(); diag('pan'); }
       else { send({type:'scroll',dx:-Math.round(mx),dy:-Math.round(my)}); diag('scroll'); }
     }
     lastC=c; lastDist=d; return;
   }
   if(!lastRaw) return;
   var tch=e.touches[0];
   var dx=tch.clientX-lastRaw.x, dy=tch.clientY-lastRaw.y;
   if(!moved && (Math.abs(dx)>4||Math.abs(dy)>4)){ moved=true; clearTimeout(holdTimer); }
   if(!moved) return;
   lastRaw={x:tch.clientX,y:tch.clientY};
   if(dragMode){
     var r=picRect();
     if(!(r.width>0&&r.height>0)) return;
     var nx=cur.x+dx*SENS/r.width, ny=cur.y+dy*SENS/r.height;
     if(!isFinite(nx)||!isFinite(ny)) return;
     cur.x=Math.min(1,Math.max(0,nx)); cur.y=Math.min(1,Math.max(0,ny));
     drawCur(); send({type:'drag',x:cur.x,y:cur.y}); return;
   }
   moveBy(dx,dy);
 },{passive:false});

 function endTouch(e){
   if(e&&e.cancelable) e.preventDefault();
   clearTimeout(holdTimer);
   if(twoFinger){twoFinger=false;lastC=null;lastDist=0;g2mode=null;return;}
   if(dragMode){ send({type:'up',x:cur.x,y:cur.y}); dot.classList.remove('press');
                 dragMode=false; lastRaw=null; return; }
   if(!lastRaw) return;
   if(moved){ lastRaw=null; return; }          // it was a pointer move; nothing to click
   var now=Date.now();
   if(now-lastTap<320){ press('dblclick'); lastTap=0; }
   else { press('click'); lastTap=now; }
   lastRaw=null;
 }
 surf.addEventListener('touchend',endTouch,{passive:false});
 // a cancelled touch is one the browser took away; treat it as an ending, or a lost gesture
 // leaves the button stuck down on the Mac.
 surf.addEventListener('touchcancel',function(e){
   clearTimeout(holdTimer);
   if(dragMode) send({type:'up',x:cur.x,y:cur.y});
   dot.classList.remove('press');
   lastRaw=null; twoFinger=false; lastC=null; lastDist=0; g2mode=null; moved=false; dragMode=false;
 },{passive:false});

 document.getElementById('lclick').onclick=function(){press('click');};
 document.getElementById('rclick').onclick=function(){press('click','right');};

 // typing: a hidden field so the phone's own keyboard (and its autofill) opens
 var kb=document.getElementById('kb');
 document.getElementById('keyb').onclick=function(){kb.focus();};
 kb.addEventListener('input',function(){ if(kb.value){ send({type:'text',text:kb.value}); kb.value=''; } });
 kb.addEventListener('keydown',function(e){
   var map={Enter:36,Backspace:51,Tab:48,Escape:53,ArrowUp:126,ArrowDown:125,ArrowLeft:123,ArrowRight:124};
   if(map[e.key]){ send({type:'key',code:map[e.key]}); e.preventDefault(); }
 });
 document.getElementById('esc').onclick=function(){send({type:'key',code:53});};
 document.getElementById('ret').onclick=function(){send({type:'key',code:36});};
 document.getElementById('tab').onclick=function(){send({type:'key',code:48});};
 document.getElementById('spot').onclick=function(){send({type:'spotlight'});};
 // a mouse works too, so the same page is usable from a laptop
 surf.addEventListener('dblclick',function(e){var p=norm(e);send({type:'dblclick',x:p.x,y:p.y});});
 surf.addEventListener('click',function(e){var p=norm(e);send({type:'click',x:p.x,y:p.y});});
 surf.addEventListener('contextmenu',function(e){e.preventDefault();var p=norm(e);
   send({type:'click',button:'right',x:p.x,y:p.y});});
 // ⚠️ SCROLL BUTTONS, BECAUSE TWO-FINGER SCROLL IS JITTERY ON A PHONE. Steady increments beat a
 // noisy gesture. dy<0 scrolls toward the top, dy>0 toward the bottom (matching the two-finger
 // scroll). Up/down repeat while held; the bar-arrows burst a long way to reach either end.
 function scrollBy(dy){ send({type:'scroll',dx:0,dy:dy}); }
 function hold(id,dy){
   var b=document.getElementById(id), iv=null;
   function go(e){ if(e&&e.cancelable)e.preventDefault(); scrollBy(dy); iv=setInterval(function(){scrollBy(dy);},90); }
   function stop(){ if(iv){clearInterval(iv);iv=null;} }
   b.addEventListener('touchstart',go,{passive:false}); b.addEventListener('touchend',stop);
   b.addEventListener('touchcancel',stop); b.addEventListener('mousedown',go);
   b.addEventListener('mouseup',stop); b.addEventListener('mouseleave',stop);
 }
 hold('sup',-140); hold('sdn',140);
 function burst(dy){ var n=0,iv=setInterval(function(){ scrollBy(dy); if(++n>=12) clearInterval(iv); },35); }
 document.getElementById('stop').addEventListener('click',function(){burst(-2600);});
 document.getElementById('sbot').addEventListener('click',function(){burst(2600);});
 var guide=document.getElementById('guide');
 try{ if(!localStorage.getItem('@@GUIDE_KEY@@')) guide.style.display='block'; }catch(e){}
 document.getElementById('help').onclick=function(){guide.style.display='block';};
 document.getElementById('guideok').onclick=function(){guide.style.display='none';try{localStorage.setItem('@@GUIDE_KEY@@','1');}catch(e){}};
})();
</script></body></html>"""

VIEW_HTML_DEFAULT = render()
