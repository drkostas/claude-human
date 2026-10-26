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
                   wa.DAY_FIRST, wa.open_chat_js("A"), wa.fill_number_js("+15550000000")):
        assert script.startswith("(function(){") and script.rstrip().endswith("})()"), \
            "each is one expression, so it runs wherever an expression's value can be read back"


def test_nothing_here_types_a_code_or_sends():
    for script in (wa.STATE, wa.LINK_BY_NUMBER, wa.USE_HERE, wa.CHATS, wa.MESSAGES, wa.OPEN_TITLE, wa.SCROLL_UP):
        low = script.lower()
        assert "send" not in low and "keydown" not in low and "keypress" not in low
