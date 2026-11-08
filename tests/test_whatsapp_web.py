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
    for script in (wa.STATE, wa.LINK_BY_NUMBER, wa.CHATS, wa.MESSAGES, wa.OPEN_TITLE, wa.SCROLL_UP,
                   wa.DAY_FIRST, wa.CLOSE_CHAT, wa.open_chat_js("A"), wa.fill_number_js("+15550000000")):
        assert script.startswith("(function(){") and script.rstrip().endswith("})()"), \
            "each is one expression, so it runs wherever an expression's value can be read back"


def test_nothing_here_types_a_code_or_sends():
    for script in (wa.STATE, wa.LINK_BY_NUMBER, wa.USE_HERE, wa.CHATS, wa.MESSAGES, wa.OPEN_TITLE, wa.SCROLL_UP):
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
    assert _shows([[None, "Design  Crew", "auto"]], "Design  Crew") is True
    assert _shows(GROUP, "Design") is False and _shows(GROUP, "Ana") is False
