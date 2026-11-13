"""Reddit chat's list of rooms read from a tab: the state, the rows and who wrote each last message, on
invented pages."""
import datetime as dt
import json
import shutil
import subprocess

import pytest

from claude_human import reddit_chat as rc


def test_the_state_is_read():
    assert rc.parse_state('{"state":"signed_in"}') == "signed_in"
    assert rc.parse_state('{"state":"signed_out"}') == "signed_out"
    assert rc.parse_state('{"state":"loading"}') == "loading"
    for bad in (None, "", "not json", '{"state":"other"}', "[]"):
        assert rc.parse_state(bad) == "unknown"


def test_who_wrote_the_last_message_is_split_off():
    assert rc.split_last("ana_example", "You: Which step fails? Please send the error.") == \
        (True, None, "Which step fails? Please send the error.")
    assert rc.split_last("ana_example", "ana_example: It fails at the upload step") == \
        (False, "ana_example", "It fails at the upload step")
    assert rc.split_last("Weekend plans", "ben-42: see you there: at 8") == (False, "ben-42", "see you there: at 8"), \
        "in a group the line names its writer, and a later colon stays in the text"
    assert rc.split_last("ana_example", "Group created") == (None, None, "Group created"), \
        "a notice names no writer"
    assert rc.split_last("ana_example", "a long sentence with a colon: in it") == \
        (None, None, "a long sentence with a colon: in it"), "words before a colon are not a user name"
    assert rc.split_last(None, None) == (None, None, "")


def test_rows_carry_the_room_its_writer_and_its_unread_count():
    raw = json.dumps([
        {"room": "!aaa:reddit.com", "name": "ana_example", "time": "12:40 PM", "last": "ana_example: Thanks, it works",
         "badge": "2"},
        {"room": "!bbb:reddit.com", "name": "ben-42", "time": "Oct 9", "last": "You: Glad it helped", "badge": ""},
        {"room": None, "name": "no id", "last": "x"},
    ])
    rows = rc.parse_rooms(raw)
    assert rows == [
        {"room": "!aaa:reddit.com", "name": "ana_example", "time": "12:40 PM", "from_me": False, "sender": "ana_example",
         "text": "Thanks, it works", "unread": 2},
        {"room": "!bbb:reddit.com", "name": "ben-42", "time": "Oct 9", "from_me": True, "sender": None,
         "text": "Glad it helped", "unread": 0},
    ], "a row without a room id is left out"
    assert [r["room"] for r in rc.waiting(rows)] == ["!aaa:reddit.com"], "only a room someone else spoke last in waits"
    assert rc.parse_rooms("null") is None and rc.parse_rooms("not json") is None and rc.parse_rooms(None) is None


def test_the_time_the_list_shows_becomes_a_day():
    now = dt.datetime(2026, 3, 4, 15, 0, tzinfo=dt.timezone.utc)
    assert rc.shown_at("12:40 PM", now) == (dt.datetime(2026, 3, 4, 12, 40, tzinfo=dt.timezone.utc), True)
    assert rc.shown_at("12:05 AM", now)[0].hour == 0
    assert rc.shown_at("14:05", now) == (dt.datetime(2026, 3, 4, 14, 5, tzinfo=dt.timezone.utc), True)
    assert rc.shown_at("Yesterday", now) == (dt.datetime(2026, 3, 3, tzinfo=dt.timezone.utc), False)
    assert rc.shown_at("Feb 9", now) == (dt.datetime(2026, 2, 9, tzinfo=dt.timezone.utc), False)
    assert rc.shown_at("Oct 9", now) == (dt.datetime(2025, 10, 9, tzinfo=dt.timezone.utc), False), \
        "a day later than today is last year's"
    assert rc.shown_at("Oct 9, 2024", now)[0].year == 2024
    for bad in (None, "", "Mon", "25:00", "Feb 30"):
        assert rc.shown_at(bad, now) == (None, False)


#: a stand-in page: the list's rooms as elements with shadow roots holding the classes Reddit draws
_PAGE = r"""
function Text(t){this.textContent=t;}
function Shadow(parts){this.parts=parts;}
Shadow.prototype.querySelector=function(sel){var k=sel.replace('.','');return k in this.parts?new Text(this.parts[k]):null;};
Shadow.prototype.querySelectorAll=function(){return [];};
function El(tag, attrs, parts){this.tagName=tag;this.attrs=attrs||{};this.shadowRoot=parts?new Shadow(parts):null;}
El.prototype.getAttribute=function(n){return n in this.attrs?this.attrs[n]:null;};
var rooms=ROOMS_IN.map(function(r){return new El('RS-ROOMS-NAV-ROOM',{room:r[0]},r[1]);});
var nav=new El('RS-ROOMS-NAV',{},{});nav.shadowRoot.querySelectorAll=function(){return rooms;};
var app=new El('RS-APP',{},{});app.shadowRoot.querySelectorAll=function(){return NAV_IN?[nav]:[];};
var document={querySelectorAll:function(){return [app];},querySelector:function(){return null;}};
var location={pathname:'/chat/room/create'};
console.log(SCRIPT);
"""


def _run(script, rooms, nav=True):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    prog = _PAGE.replace("ROOMS_IN", json.dumps(rooms)).replace("NAV_IN", "true" if nav else "false") \
        .replace("SCRIPT", script)
    run = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def test_the_rooms_are_read_through_the_shadow_roots():
    rooms = [["!aaa:reddit.com", {"room-name": "ana_example", "last-message-time": "12:40 PM",
                                  "last-message": " ana_example:  It fails\n at upload ", "badges": "1"}],
             ["!bbb:reddit.com", {"room-name": "ben-42", "last-message-time": "Oct 9",
                                  "last-message": "You: Glad it helped"}],
             ["!ccc:reddit.com", {}]]
    rows = rc.parse_rooms(_run(rc.ROOMS, rooms))
    assert [(r["room"], r["from_me"], r["sender"], r["text"], r["unread"]) for r in rows] == [
        ("!aaa:reddit.com", False, "ana_example", "It fails at upload", 1),
        ("!bbb:reddit.com", True, None, "Glad it helped", 0)], "a row not drawn yet is left out"
    assert rc.parse_rooms(_run(rc.ROOMS, [["!ccc:reddit.com", {}]])) is None, "nothing drawn reads as no list"


def test_the_state_script_sees_the_list():
    assert rc.parse_state(_run(rc.STATE, [], nav=True)) == "signed_in"
    assert rc.parse_state(_run(rc.STATE, [], nav=False)) == "loading"


def test_nothing_here_clicks_types_or_opens_a_room():
    for script in (rc.STATE, rc.ROOMS):
        for word in (".click(", "dispatchEvent", ".value=", "location.assign", "location.href=", "submit"):
            assert word not in script, word
    assert rc.URL.endswith("/chat/room/create"), "the page that opens no room"
