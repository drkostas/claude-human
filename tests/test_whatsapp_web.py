"""WhatsApp Web read from a tab: the state, the chat list and the messages as rows, from invented pages."""
import datetime as dt
import json

import pytest

from claude_human import whatsapp_web as wa


def test_the_state_is_read_and_a_code_is_shown_in_two_halves():
    assert wa.parse_state('{"state":"code","code":"ABCD1234","by_number":true}') == \
        {"state": "code", "code": "ABCD-1234", "by_number": True, "qr": False}
    assert wa.parse_state('{"state":"signed_out","code":null,"by_number":true,"qr":true}')["qr"] is True, \
        "the QR has loaded, so the phone-number link can be pressed"
    assert wa.parse_state('{"state":"signed_in","code":null,"by_number":false}')["state"] == "signed_in"
    assert wa.parse_state('{"state":"number","code":null,"by_number":true}')["state"] == "number"
    assert wa.parse_state('{"state":"signed_out","code":"ABCD1234"}')["code"] is None, \
        "a code counts only on the code screen"
    for bad in (None, "", "not json", '{"state":"other"}', "[]"):
        assert wa.parse_state(bad)["state"] == "unknown"


def test_whatsapp_open_in_another_window_is_its_own_state_and_use_here_takes_it():
    assert wa.parse_state('{"state":"elsewhere","code":null,"by_number":false,"qr":false}')["state"] == "elsewhere"
    body = wa.STATE[wa.STATE.index("if(chatList())"):]
    assert body.index("if(chatList())") < body.index("byWords(['Use here'])") < body.index("linkCode()"), \
        "the chat list first, then the page another window took over, then the link screens"
    assert "byWords(['Use here'])" in wa.USE_HERE and ".click()" in wa.USE_HERE
    assert wa.USE_HERE.startswith("(function(){") and wa.USE_HERE.rstrip().endswith("})()")


def test_the_line_before_a_message_gives_its_time_and_sender():
    assert wa.parse_prefix("[21:03, 25/10/2026] Alice Example: ") == (dt.datetime(2026, 10, 25, 21, 3), "Alice Example")
    assert wa.parse_prefix("[9:03 PM, 10/25/2026] Bob: ") == (dt.datetime(2026, 10, 25, 21, 3), "Bob")
    assert wa.parse_prefix("[12:15 a.m., 10/25/2026] Bob: ")[0] == dt.datetime(2026, 10, 25, 0, 15)
    assert wa.parse_prefix("[09:03, 05/10/2026] Alice: ")[0] == dt.datetime(2026, 10, 5, 9, 3)
    assert wa.parse_prefix("[09:03, 05/10/2026] Alice: ", day_first=False)[0] == dt.datetime(2026, 5, 10, 9, 3), \
        "only the browser's language decides 05/10"
    assert wa.parse_prefix("[21.03, 25.10.26] Alice: ")[0] == dt.datetime(2026, 10, 25, 21, 3)
    assert wa.parse_prefix("[21:03:09, 2026-10-25] Alice: ")[0] == dt.datetime(2026, 10, 25, 21, 3, 9)
    assert wa.parse_prefix("[21:03, 25/10/2026] Team: Q4: ") == (dt.datetime(2026, 10, 25, 21, 3), "Team: Q4"), \
        "a name with a colon in it keeps it"
    assert wa.parse_prefix("[25:03, 25/10/2026] Alice: ")[0] is None
    assert wa.parse_prefix("[21:03, 31/02/2026] Alice: ") == (None, "Alice"), "a date that does not exist is None"
    for bad in (None, "", "Alice: hello", "[21:03] Alice: "):
        assert wa.parse_prefix(bad) == (None, None)


def test_a_message_id_names_the_chat_who_sent_it_and_a_group_member():
    assert wa.split_id("false_15550000001@c.us_3EB0AAA1") == \
        {"from_me": False, "chat": "15550000001@c.us", "message": "3EB0AAA1", "participant": None}
    assert wa.split_id("true_15550000001@c.us_3EB0AAA2")["from_me"] is True
    assert wa.split_id("false_120363000000000000@g.us_3EB0AAA3_15550000002@c.us") == \
        {"from_me": False, "chat": "120363000000000000@g.us", "message": "3EB0AAA3",
         "participant": "15550000002@c.us"}
    for bad in ("", "quoted_inner", "maybe_15550000001@c.us_X", "false_nochat_X"):
        assert wa.split_id(bad) is None


def test_messages_become_rows_with_the_browsers_zone():
    zone = dt.timezone(dt.timedelta(hours=2))
    raw = json.dumps([
        {"id": "false_15550000001@c.us_3EB0AAA1", "raw": "[21:01, 25/10/2026] Alice: ", "text": "Are you coming?",
         "kind": "text"},
        {"id": "true_15550000001@c.us_3EB0AAA2", "raw": "[21:02, 25/10/2026] Me: ", "text": "Yes", "kind": "text"},
        {"id": "false_15550000001@c.us_3EB0AAA3", "raw": None, "text": "", "kind": "Voice message"},
        {"id": "quoted_inner", "raw": None, "text": "", "kind": "other"}])
    rows = wa.parse_messages(raw, tz=zone)
    assert [r["id"] for r in rows] == ["3EB0AAA1", "3EB0AAA2", "3EB0AAA3"], "an id that is not a message is left out"
    assert rows[0] == {"id": "3EB0AAA1", "chat": "15550000001@c.us", "from_me": False, "sender": "Alice",
                       "text": "Are you coming?", "sent_at": "2026-10-25T21:01:00+02:00", "kind": "text",
                       "raw": "[21:01, 25/10/2026] Alice: "}
    assert rows[1]["from_me"] is True and rows[2]["sent_at"] is None and rows[2]["kind"] == "Voice message"
    assert wa.parse_messages(raw)[0]["sent_at"] == "2026-10-25T21:01:00", "with no zone given, none is added"
    assert wa.parse_messages("null") is None and wa.parse_messages("oops") is None


def test_the_browsers_date_order_is_read():
    assert wa.parse_day_first("true") is True and wa.parse_day_first("false") is False
    assert wa.parse_day_first("null") is True and wa.parse_day_first(None, default=False) is False
    assert wa.DAY_FIRST.startswith("(function(){") and "formatToParts" in wa.DAY_FIRST


def test_the_chat_list_is_read():
    raw = json.dumps([{"title": "Alice", "time": "21:03", "preview": "See you", "unread": 2},
                      {"title": None, "time": None}, {"title": "Team", "time": "Yesterday", "preview": None}])
    assert wa.parse_chats(raw) == [{"title": "Alice", "time": "21:03", "preview": "See you", "unread": 2},
                                   {"title": "Team", "time": "Yesterday", "preview": None, "unread": 0}]
    assert wa.parse_chats("null") is None


def test_the_scripts_carry_their_values_as_json_and_refuse_a_number_that_is_not_one():
    js = wa.open_chat_js('Team "Q4" \\ </script>')
    assert json.dumps('Team "Q4" \\ </script>') in js, "a title goes in as a JSON string, never as code"
    assert json.dumps("+15550000000") in wa.fill_number_js("+15550000000")
    assert json.dumps("+1 555 000 0000") in wa.fill_number_js("+1 555 000 0000")
    for bad in ("", "abc", "+1555'); alert(1); ('", "12345", "+" + "1" * 30,
                "5550000000"):   # without its +, the form reads another country's code
        with pytest.raises(ValueError):
            wa.fill_number_js(bad)
    for script in (wa.STATE, wa.LINK_BY_NUMBER, wa.CHATS, wa.MESSAGES, wa.OPEN_TITLE, wa.SCROLL_UP, wa.SCROLL_STEP,
                   wa.DAY_FIRST, wa.CLOSE_CHAT, wa.open_chat_js("A"), wa.fill_number_js("+15550000000")):
        assert script.startswith("(function(){") and script.rstrip().endswith("})()"), \
            "each is one expression, so it runs wherever an expression's value can be read back"


def test_nothing_here_types_a_code_or_sends():
    for script in (wa.STATE, wa.LINK_BY_NUMBER, wa.USE_HERE, wa.CHATS, wa.MESSAGES, wa.OPEN_TITLE, wa.SCROLL_UP,
                   wa.SCROLL_STEP):
        low = script.lower()
        assert "send" not in low and "keydown" not in low and "keypress" not in low


def test_closing_the_open_chat_presses_only_escape_and_reloads_when_a_chat_stays_open():
    js = wa.CLOSE_CHAT
    assert js.index("if(!document.querySelector('#main'))") < js.index("Escape"), \
        "with no chat open it presses nothing"
    assert js.count("new KeyboardEvent(") == 1 and "key:'Escape'" in js and "keyCode:27" in js, \
        "one key, Escape, sent down and up"
    assert "setTimeout(" in js and "location.reload()" in js, "a chat still open is closed by a reload"
    low = js.lower()
    assert "send" not in low and "click" not in low and "keypress" not in low


_STAND_IN = r"""
var got=[];
function El(name){this.name=name;}
El.prototype.dispatchEvent=function(e){got.push({on:this.name,type:e.type,o:e.o});return true;};
function Ev(t,o){this.type=t;this.o=o;}
var MouseEvent=Ev;var window={PointerEvent:function(t,o){this.type=t;this.o=o;this.pointer=true;}};
var cell=new El('cell');
var row=new El('row');row.getBoundingClientRect=function(){return {left:10,top:200,width:300,height:60};};
row.contains=function(e){return e===cell||e===row;};row.scrolled=false;
row.scrollIntoView=function(){row.scrolled=true;};
var span={getAttribute:function(){return WANT;},closest:function(){return row;}};
var other={getAttribute:function(){return 'Someone else';},closest:function(){return new El('other');}};
var list={querySelectorAll:function(){return [other,span];}};
var document={querySelector:function(q){return q==='#pane-side'?list:null;},
  elementFromPoint:function(x,y){got.push({at:[x,y]});return cell;}};
var out=SCRIPT;
console.log(JSON.stringify({out:JSON.parse(out),got:got,scrolled:row.scrolled}));
"""


def test_a_row_is_pressed_with_the_pointer_sequence_a_real_mouse_sends():
    # WhatsApp Web ignores a bare mousedown, mouseup and click on a row, so a chat never opened
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    title = 'Team "Q4"'
    prog = _STAND_IN.replace("WANT", json.dumps(title)).replace("SCRIPT", wa.open_chat_js(title))
    run = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    res = json.loads(run.stdout)
    assert res["out"] == {"opened": True} and res["scrolled"] is True
    assert res["got"][0] == {"at": [160, 230]}, "the element at the row's centre is the one pressed"
    events = res["got"][1:]
    assert [e["type"] for e in events] == ["pointerover", "mouseover", "mousemove", "pointerdown",
                                           "mousedown", "pointerup", "mouseup", "click"]
    assert all(e["on"] == "cell" for e in events), "only that row's element is pressed"
    for e in events:
        o = e["o"]
        assert o["pointerId"] == 1 and o["pointerType"] == "mouse" and o["isPrimary"] is True
        assert (o["clientX"], o["clientY"]) == (160, 230) and o["bubbles"] is True
        assert o["buttons"] == (1 if e["type"] in ("pointerdown", "mousedown") else 0)


_HEADER = r"""
function Span(title, text, dir){this.t=title;this.text=text;this.dir=dir||null;this.innerText=text;}
Span.prototype.getAttribute=function(n){return n==='title'?this.t:n==='dir'?this.dir:null;};
var raw=SPANS;var spans=raw===null?null:raw.map(function(s){return new Span(s[0],s[1],s[2]);});
var header=spans===null?null:{querySelectorAll:function(q){return q==='span'?spans:[];},
  querySelector:function(q){for(var i=0;i<spans.length;i++){var s=spans[i];
    if(q==='span[title]'&&s.t!==null)return s;if(q==='span[dir=auto]'&&s.dir==='auto')return s;}return null;}};
var document={querySelector:function(q){return q==='#main header'?header:null;}};
console.log(SCRIPT);
"""


def _shows(spans, title):
    """What open_title_js answers on a stand-in header of (title attribute, text, dir) spans."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    prog = _HEADER.replace("SPANS", json.dumps(spans)).replace("SCRIPT", wa.open_title_js(title))
    run = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


#: a group's header: its name has no title attribute, and the member list before it in the
#: selector's order does (invented names and an example number)
GROUP = [["Ana, Ben, +1 555 0100, You", "Ana, Ben, +1 555 0100, You", None], [None, "Design Crew", "auto"]]


def test_a_group_chat_is_known_open_by_its_name_not_its_member_list():
    assert _shows(GROUP, "Design Crew") is True


def test_a_one_to_one_chat_is_known_open_by_its_name():
    assert _shows([["Ana Lima", "Ana Lima", "auto"], [None, "online", None]], "Ana Lima") is True


def test_a_header_still_showing_the_previous_chat_is_not_the_chat_asked_for():
    assert _shows([["Old Chat", "Old Chat", "auto"], [None, "last seen today", None]], "Design Crew") is False
    assert _shows(None, "Design Crew") is False, "and no chat open is not open"


def test_the_title_is_compared_whole_after_folding_spaces_never_as_a_part():
    nbsp = "\u00a0"
    assert _shows([[None, "Design" + nbsp + " Crew", "auto"]], "Design Crew") is True, \
        "text with a no-break space and a space is the title with one space"
    assert _shows([["Design" + nbsp + "Crew", "x", None]], "Design   Crew") is True, \
        "and a title attribute with a no-break space is the title with three spaces"
    assert _shows([[None, " Design Crew\n", "auto"]], "Design Crew") is True, "and spaces at its ends fold away"
    assert _shows([[None, "DesignCrew", "auto"]], "Design Crew") is False, "but a missing space is another title"
    assert _shows(GROUP, "Design") is False and _shows(GROUP, "Ana") is False


def test_a_message_id_is_read_in_its_long_form_as_a_bare_id_or_not_at_all():
    assert wa.split_id("false_15550000001@c.us_3EB0AAA1")["chat"] == "15550000001@c.us"
    assert wa.split_id("3EB0A1B2C3D4E5F6A7B8C9") == \
        {"from_me": None, "chat": None, "message": "3EB0A1B2C3D4E5F6A7B8C9", "participant": None}, \
        "a bare id names no chat and no sender, so both stay unknown"
    for bad in ("", None, "3EB0", "3EB0 A1B2C3D4E5F6", "conv-msg-1"):
        assert wa.split_id(bad) is None


def test_a_bare_id_row_takes_the_chat_the_caller_opened_and_never_a_guess_at_who_sent_it():
    raw = json.dumps([{"id": "3EB0A1B2C3D4E5F6A7B8C9", "raw": "[10:01, 05/11/2026] Ana: ", "text": "Hello",
                       "kind": "text"}])
    row = wa.parse_messages(raw, chat="120363000000000001@g.us")[0]
    assert row["id"] == "3EB0A1B2C3D4E5F6A7B8C9" and row["chat"] == "120363000000000001@g.us"
    assert row["from_me"] is None and row["sender"] == "Ana"
    assert wa.parse_messages(raw)[0]["chat"] is None


#: a stand-in page in the shape a newer WhatsApp Web draws in a tab that is not shown: each drawn message
#: is a wrapper of 87 px (data-virtualized) around a node with a bare id, whose copyable line sits 9
#: levels below it under a msg-container; the element that scrolls is above the wrappers, and setting
#: its scrollTop draws the DRAWN messages that end at the bottom of the view. Invented names and text.
_PAGE = r"""
var H=87,N=COUNT,DRAWN=PERDRAW,CLIENT=600,SCROLLS=SCROLLS_OK;
function El(tag,attrs,cls){this.tag=tag;this.attrs=attrs||{};this.cls=cls||[];this.children=[];
this.parentElement=null;this.innerText='';this.offsetHeight=0;this.clientHeight=0;this.scrollHeight=0;this.style={};}
El.prototype.add=function(c){c.parentElement=this;this.children.push(c);return c;};
El.prototype.getAttribute=function(n){return n in this.attrs?this.attrs[n]:null;};
El.prototype.matches=function(q){if(q==='#main')return this.attrs.id==='main';
if(q==='span.selectable-text')return this.tag==='span'&&this.cls.indexOf('selectable-text')>=0;
var m=/^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(q);if(!m)throw new Error('selector '+q);
return m[2]===undefined?(m[1] in this.attrs):this.attrs[m[1]]===m[2];};
El.prototype.querySelectorAll=function(q){var out=[];(function walk(e){for(var i=0;i<e.children.length;i++){
var c=e.children[i];if(c.matches(q))out.push(c);walk(c);}})(this);return out;};
El.prototype.querySelector=function(q){return this.querySelectorAll(q)[0]||null;};
El.prototype.closest=function(q){var e=this;while(e){if(e.matches(q))return e;e=e.parentElement;}return null;};
function id(i){var h=(0x3EB0000000+i).toString(16).toUpperCase();while(h.length<22)h='A'+h;return h;}
var main=new El('div',{id:'main'});main.add(new El('header'));
var list=main.add(new El('div'));list.style.overflowY=SCROLLS?'auto':'visible';list.clientHeight=CLIENT;
list.scrollHeight=SCROLLS?N*H:CLIENT;var top=SCROLLS?N*H-CLIENT:0;
function draw(){list.children=[];var last=Math.min(N-1,Math.floor((top+CLIENT-1)/H));
for(var i=Math.max(0,last-DRAWN+1);i<=last;i++){
var w=list.add(new El('div',{'data-virtualized':'true'}));w.offsetHeight=H;w.clientHeight=H;w.scrollHeight=H;
var m=w.add(new El('div',{'data-id':id(i)}));m.offsetHeight=H;var p=m;
for(var k=1;k<=8;k++)p=p.add(new El('div',k===2?{'data-testid':'msg-container'}:{}));
var line=p.add(new El('div',{'data-pre-plain-text':'[10:'+(i<10?'0':'')+(i%60)+', 05/11/2026] Ana: '}));
var s=line.add(new El('span',{},['selectable-text']));s.innerText='Message '+i;}}
Object.defineProperty(list,'scrollTop',{get:function(){return top;},set:function(v){top=Math.max(0,Math.min(v,list.scrollHeight-CLIENT));draw();}});
draw();
var document={querySelector:function(q){return q==='#main'?main:null;}};
function getComputedStyle(e){return {overflowY:e.style.overflowY||'visible'};}
var rl=require('readline').createInterface({input:process.stdin});
rl.on('line',function(l){var out;try{out=eval(JSON.parse(l));}catch(e){out=JSON.stringify({error:String(e)});}
process.stdout.write(JSON.stringify(out)+'\n');});
"""


class _Tab:
    """A node process holding _PAGE; calling it runs a script there and returns the script's string."""

    def __init__(self, count, per_draw, scrolls=True):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        prog = (_PAGE.replace("COUNT", str(count)).replace("PERDRAW", str(per_draw))
                .replace("SCROLLS_OK", "true" if scrolls else "false"))
        self.p = subprocess.Popen([node, "-e", prog], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def __call__(self, script):
        self.p.stdin.write(json.dumps(script) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def _ids(raw):
    return [r["id"] for r in json.loads(raw)]


def test_messages_reads_a_bare_id_far_above_its_line_on_the_newer_page():
    tab = _Tab(30, 1)
    try:
        rows = wa.parse_messages(tab(wa.MESSAGES), chat="120363000000000001@g.us")
    finally:
        tab.close()
    assert len(rows) == 1, "the one message the hidden tab draws is read"
    assert len(rows[0]["id"]) == 22 and rows[0]["from_me"] is None and rows[0]["sender"] == "Ana"
    assert rows[0]["text"] == "Message 29" and rows[0]["chat"] == "120363000000000001@g.us"


@pytest.mark.parametrize("per_draw", [1, 7])
def test_collect_gathers_messages_by_id_while_the_drawn_window_moves(per_draw):
    tab = _Tab(30, per_draw)
    try:
        raw, how = wa.collect(tab, 20, max_steps=40, wait_s=0, sleep=lambda s: None)
        everything = _ids(raw)
    finally:
        tab.close()
    texts = [r["text"] for r in json.loads(raw)]
    assert len(everything) >= 20 and len(set(everything)) == len(everything), "each message once"
    want = [f"Message {i}" for i in range(30 - len(texts), 30)]
    assert texts == want, "oldest first, and no message between two windows is skipped"
    assert how["stopped"] == "enough" and how["scroller"] == 1 and how["first"] == per_draw
    assert how["depth"] == 11, "nine levels to the id, one to its wrapper, one to the list"
    assert how["client_height"] == 600 and how["scroll_height"] == 30 * 87
    assert all(isinstance(n, int) for n in how["gained"]) and how["steps"] == len(how["gained"])
    if per_draw == 1:
        assert how["gained"] == [1] * 19, "a tab that draws one message moves one message a step"


def test_collect_stops_at_the_top_with_every_message():
    tab = _Tab(12, 7)
    try:
        raw, how = wa.collect(tab, 50, wait_s=0, sleep=lambda s: None)
    finally:
        tab.close()
    assert [r["text"] for r in json.loads(raw)] == [f"Message {i}" for i in range(12)]
    assert how["stopped"] == "top"


def test_collect_says_when_nothing_scrolls_and_keeps_what_it_read():
    tab = _Tab(5, 3, scrolls=False)
    try:
        raw, how = wa.collect(tab, 20, wait_s=0, sleep=lambda s: None)
    finally:
        tab.close()
    assert len(_ids(raw)) == 3 and how["stopped"] == "no_scroller" and how["scroller"] == 0 and how["steps"] == 0


def test_collect_with_no_chat_open_reads_nothing():
    raw, how = wa.collect(lambda s: "null", 20, sleep=lambda s: None)
    assert raw is None and how["stopped"] == "no_chat" and how["first"] == 0


def test_collect_reports_numbers_only_besides_its_stop():
    raw, how = wa.collect(lambda s: "[]" if s == wa.MESSAGES else '{"found":true,"depth":"x","top_before":5,"top_after":5}',
                          3, sleep=lambda s: None)
    assert how["stopped"] in wa.STOPS and how["depth"] is None
    for k, v in how.items():
        assert k == "stopped" or v is None or isinstance(v, (int, list))
