"""OCR lines picked by words and place, and the station client's requests, without a screen."""
import io
import json
import urllib.error

from claude_human import ocr
from claude_human.station import client

FOUND = [
    {"text": "Search", "x": 0.05, "y": 0.04, "w": 0.1, "h": 0.03},
    {"text": "Parcel  co", "x": 0.06, "y": 0.20, "w": 0.15, "h": 0.03},
    {"text": "Parcel Co", "x": 0.45, "y": 0.03, "w": 0.15, "h": 0.03},
    {"text": "Your parcel waits", "x": 0.45, "y": 0.50, "w": 0.3, "h": 0.03},
]


def test_find_matches_words_exactly_and_by_side():
    assert [d["x"] for d in ocr.find(FOUND, "parcel co")] == [0.06, 0.45], "spaces and case aside"
    assert [d["x"] for d in ocr.find(FOUND, "Parcel Co", right=0.35)] == [0.06], "the left column only"
    assert ocr.find(FOUND, "Parcel") == []


def test_in_band_and_middle():
    top = ocr.in_band(FOUND, 0.0, 0.1, left=0.35)
    assert [d["text"] for d in top] == ["Parcel Co"], "the header band on the right"
    assert ocr.middle(FOUND[0]) == (0.1, 0.055)


def test_reading_order_is_rows_then_left_to_right():
    rows = ocr._order([{"text": "b", "x": 0.5, "y": 0.104, "w": 0.1, "h": 0.02},
                       {"text": "a", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.02},
                       {"text": "c", "x": 0.1, "y": 0.3, "w": 0.1, "h": 0.02}])
    assert [d["text"] for d in rows] == ["a", "b", "c"]


def test_lines_of_something_unreadable_is_none(tmp_path):
    bad = tmp_path / "x.png"
    bad.write_bytes(b"not a picture")
    assert ocr.lines(bad) in (None, [])


class Resp(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def recorder(answer: bytes = b'{"ok": true}', headers=None):
    seen = []

    def opener(req, timeout=None):
        seen.append(req)
        return Resp(answer, headers)
    return seen, opener


def test_click_posts_the_station_input_with_the_token():
    seen, opener = recorder()
    assert client.click(42, 0.25, 0.5, token="t0k", station="http://192.0.2.1:8789/", opener=opener) == {"ok": True}
    req = seen[0]
    assert req.full_url == "http://192.0.2.1:8789/input" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer t0k"
    assert json.loads(req.data) == {"type": "click", "window": 42, "x": 0.25, "y": 0.5}
    client.click(42, 0.1, 0.1, double=True, token="t", opener=opener)
    assert json.loads(seen[1].data)["type"] == "dblclick"


def test_scroll_key_and_text_messages():
    seen, opener = recorder()
    client.scroll(7, 300, token="t", opener=opener)
    client.key(7, 53, token="t", opener=opener)
    client.text(7, "hello", token="t", opener=opener)
    assert [json.loads(r.data)["type"] for r in seen] == ["scroll", "key", "text"]
    assert json.loads(seen[0].data)["dy"] == 300 and json.loads(seen[1].data)["code"] == 53


def test_a_refusal_keeps_the_stations_words_and_an_unreachable_station_says_so():
    def refusing(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "no", {}, io.BytesIO(b'{"detail": "covered"}'))
    assert client.click(1, 0.5, 0.5, token="t", opener=refusing) == {"ok": False, "detail": "covered"}

    def down(req, timeout=None):
        raise OSError("refused")
    got = client.click(1, 0.5, 0.5, token="t", opener=down)
    assert got["ok"] is False and "did not answer" in got["detail"]


def test_shot_returns_the_jpeg_and_its_size():
    seen, opener = recorder(b"\xff\xd8jpeg", {"X-Frame-Size": "1085x682"})
    assert client.shot(9, token="t", opener=opener) == (b"\xff\xd8jpeg", 1085, 682)
    assert seen[0].full_url.endswith("/shot?window=9") and seen[0].get_method() == "GET"
    client.shot(9, token="t", full=True, opener=opener)
    assert seen[1].full_url.endswith("/shot?window=9&full=1"), "a full shot is asked for by name"

    def missing(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "no", {}, io.BytesIO(b"{}"))
    assert client.shot(9, token="t", opener=missing) is None


def test_lines_reads_a_drawn_picture_with_its_place(tmp_path):
    import pytest
    appkit = pytest.importorskip("AppKit")
    pytest.importorskip("Vision")
    from Foundation import NSMakePoint, NSMakeRect, NSMakeSize
    img = appkit.NSImage.alloc().initWithSize_(NSMakeSize(600, 300))
    img.lockFocus()
    appkit.NSColor.whiteColor().set()
    appkit.NSRectFill(NSMakeRect(0, 0, 600, 300))
    attrs = {appkit.NSFontAttributeName: appkit.NSFont.systemFontOfSize_(28),
             appkit.NSForegroundColorAttributeName: appkit.NSColor.blackColor()}
    appkit.NSString.stringWithString_("Parcel Co").drawAtPoint_withAttributes_(NSMakePoint(40, 230), attrs)
    appkit.NSString.stringWithString_("Bottom line").drawAtPoint_withAttributes_(NSMakePoint(40, 60), attrs)
    img.unlockFocus()
    rep = appkit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    path = tmp_path / "drawn.png"
    rep.representationUsingType_properties_(appkit.NSPNGFileType, None).writeToFile_atomically_(str(path), True)
    got = ocr.lines(path)
    assert [d["text"] for d in got] == ["Parcel Co", "Bottom line"], "top first: the origin is the top left"
    assert got[0]["y"] < 0.3 < 0.6 < got[1]["y"]
