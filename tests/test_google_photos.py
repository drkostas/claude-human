"""google_photos' scripts, run in node against a stand-in for a signed-in Google Photos page: its HTML
carries the data token, its data endpoint answers the four read calls from a small library, and each
item's original is served from its own address."""
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess

import pytest

from claude_human import google_photos as gp

_PAGE = r"""
globalThis.window = globalThis;
let SIGNED_IN = true;
const LIB = [];            // {id, t, tz, name, size, w, h, words, bytes, url, badsize}
const CALLS = [];
const PAGE = 2;            // items per result page, so paging is exercised
globalThis.document = { documentElement: { get innerHTML() {
  return SIGNED_IN ? '<script>WIZ_global_data={"SNlM0e":"tok-at","FdrFJe":"-123","cfb2h":"boq_build"};</script>' : '<html></html>';
} } };
function wrb(id, data) { return ")]}'\n\n123\n" + JSON.stringify([["wrb.fr", id, data === null ? null : JSON.stringify(data), null, null, null, "generic"]]) + "\n25\n[[\"e\",4]]\n"; }
function row(x) { return [x.id, ["https://lh3.example.org/" + x.id, x.w, x.h, null, null, null], x.t, "dedup", x.tz, x.t + 5]; }
function pageOf(list, pageId) { const at = pageId ? Number(pageId.slice(1)) : 0;
  const part = list.slice(at, at + PAGE); const next = at + PAGE < list.length ? "p" + (at + PAGE) : null; return [part.map(row), next]; }
globalThis.fetch = async (url, opts) => {
  if (String(url).startsWith('/_/PhotosUi/data/batchexecute')) {
    const q = new URLSearchParams(opts.body); const req = JSON.parse(q.get('f.req'))[0][0];
    const id = req[0], args = JSON.parse(req[1]); CALLS.push(id);
    if (q.get('at') !== 'tok-at') return { status: 401, text: async () => '' };
    const newest = LIB.slice().sort((a, b) => b.t - a.t);
    let data = null;
    if (id === 'lcxiM') {
      if (args[0]) { const [p, before] = args[0].split(':'); data = pageOf(newest.filter(x => x.t < Number(before)), p);
        if (data[1]) data[1] += ':' + before; }
      else { data = pageOf(newest.filter(x => x.t < args[1]), null); if (data[1]) data[1] += ':' + args[1]; } }
    if (id === 'EzkLib') data = pageOf(newest.filter(x => x.words.includes(args[0])), args[2]);
    const one = LIB.find(x => x.id === args[0]);
    if (id === 'fDcn4b') data = one ? [[one.id, "", one.name, one.t, one.tz, one.badsize || one.size, one.w, one.h]] : null;
    if (id === 'VrseUb') data = one ? [[one.id], one.url] : null;
    return { status: 200, text: async () => wrb(id, data) };
  }
  const x = LIB.find(y => y.url === url);
  if (!x) return { status: 404, headers: { get: () => null }, arrayBuffer: async () => new ArrayBuffer(0) };
  return { status: 200, headers: { get: k => k === 'content-type' ? 'image/jpeg' : null }, arrayBuffer: async () => x.bytes.slice().buffer };
};
window.add = o => { const bytes = new TextEncoder().encode(o.text); LIB.push({ ...o, bytes, size: bytes.length,
  w: 30, h: 40, url: 'https://originals.example.org/' + o.id + '?k=capability' }); return 'ok'; };
window.signOut = () => { SIGNED_IN = false; return 'ok'; };
window.calls = () => JSON.stringify(CALLS);
const rl = require('readline').createInterface({ input: process.stdin });
rl.on('line', l => { let out; try { out = eval(JSON.parse(l)); } catch (e) { out = JSON.stringify({ error: String(e) }); }
  process.stdout.write(JSON.stringify(out === undefined ? null : out) + '\n'); });
"""

UTC = dt.timezone.utc
OCT1 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone(dt.timedelta(hours=3)))
NOV1 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def ms(y, m, d, h=12):
    return int(dt.datetime(y, m, d, h, tzinfo=UTC).timestamp() * 1000)


class _Tab:
    """A node process holding the stand-in page; calling it runs one script and returns its string."""

    def __init__(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        self.p = subprocess.Popen([node, "-e", _PAGE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.calls = []

    def __call__(self, script):
        self.calls.append(script)
        self.p.stdin.write(json.dumps(script) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def add(self, id_, t, words=(), text="jpeg bytes", tz=7200000, **more):
        o = {"id": id_, "t": t, "tz": tz, "name": f"IMG_{id_[-4:]}.jpg", "words": list(words), "text": text, **more}
        assert self(f"add({json.dumps(o)})") == "ok"

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


@pytest.fixture
def tab():
    t = _Tab()
    t.add("AF1QipAAAAAAAAAAAAAAAAsep30", ms(2026, 9, 30, 20), ["receipt"])     # 23:00 at UTC+3, still September
    t.add("AF1QipBBBBBBBBBBBBBBBBoct01", ms(2026, 9, 30, 22), ["receipt"])     # 01:00 on 1 Oct at UTC+3
    t.add("AF1QipCCCCCCCCCCCCCCCCoct12", ms(2026, 10, 12), ["receipt", "fuel"])
    t.add("AF1QipDDDDDDDDDDDDDDDDoct20", ms(2026, 10, 20), ["beach"])
    t.add("AF1QipEEEEEEEEEEEEEEEEoct31", ms(2026, 10, 31), ["receipt"])
    t.add("AF1QipFFFFFFFFFFFFFFFFnov02", ms(2026, 11, 2), ["receipt"])
    yield t
    t.close()


def _search(tab, **kw):
    return gp.search(tab, OCT1, NOV1, step_s=0.01, wait_s=20, **kw)


def test_a_word_search_returns_only_the_range_newest_first_with_metadata_only(tab):
    got = _search(tab, words="receipt")
    assert got["ok"], got
    assert [i["id"][-5:] for i in got["items"]] == ["oct31", "oct12", "oct01"]
    first = got["items"][0]
    assert first["name"] == "IMG_ct31.jpg" and first["size"] == len("jpeg bytes")
    assert first["taken"] == "2026-10-31T14:00:00+02:00"
    assert set(first) == {"id", "name", "taken", "size", "width", "height"}
    assert "example.org" not in json.dumps(got), "no address of a picture or an original is returned"
    assert got["pages"] >= 3 and got["truncated"] is False


def test_a_date_listing_pages_backwards_and_stops_past_the_start(tab):
    got = _search(tab)
    assert [i["id"][-5:] for i in got["items"]] == ["oct31", "oct20", "oct12", "oct01"]
    assert "sep30" not in json.dumps(got)


def test_a_search_cut_by_its_most_items_or_pages_says_so(tab):
    got = _search(tab, max_items=2)
    assert len(got["items"]) == 2 and got["truncated"] is True
    assert _search(tab, max_pages=1)["truncated"] is True


def test_only_read_calls_reach_the_data_endpoint(tab):
    _search(tab, words="receipt")
    _search(tab)
    gp.fetch(tab, "AF1QipCCCCCCCCCCCCCCCCoct12", step_s=0.01, wait_s=20)
    called = set(json.loads(tab("calls()")))
    assert called == set(gp.READ_CALLS) == {"lcxiM", "EzkLib", "fDcn4b", "VrseUb"}
    for script in (gp.search_js("ab12cd34", OCT1, NOV1), gp.fetch_js("ab12cd34", "AF1QipCCCCCCCCCCCCCCCCoct12")):
        assert "READ.indexOf(id)<0" in script
        named = set(re.findall(r"rpc\('([A-Za-z0-9]+)'", script))
        assert named and named <= set(gp.READ_CALLS), named


def test_an_original_is_fetched_whole_and_checked(tab):
    tab.add("AF1QipGGGGGGGGGGGGGGGGbig01", ms(2026, 10, 15), text="x" * 5000)
    got = gp.fetch(tab, "AF1QipGGGGGGGGGGGGGGGGbig01", chunk=700, step_s=0.01, wait_s=20)
    assert got["ok"], got
    assert got["data"] == b"x" * 5000
    meta = got["meta"]
    assert meta["sha256"] == hashlib.sha256(b"x" * 5000).hexdigest()
    assert meta["size"] == 5000 and meta["mimetype"] == "image/jpeg" and meta["taken"] == "2026-10-15T14:00:00+02:00"
    assert "example.org" not in json.dumps(meta)
    assert sum("subarray" in c for c in tab.calls) == 8, "read back in chunks of 700"


def test_a_file_of_another_size_than_its_record_is_refused(tab):
    tab.add("AF1QipHHHHHHHHHHHHHHHHbad01", ms(2026, 10, 16), badsize=999)
    got = gp.fetch(tab, "AF1QipHHHHHHHHHHHHHHHHbad01", step_s=0.01, wait_s=20)
    assert got["ok"] is False and got["why"] == "size"


def test_an_unknown_item_and_a_signed_out_tab_say_so(tab):
    got = gp.fetch(tab, "AF1QipZZZZZZZZZZZZZZZZnone1", step_s=0.01, wait_s=20)
    assert got["ok"] is False and got["why"] == "no_item"
    tab("signOut()")
    assert _search(tab)["why"] == "not_signed_in"
    assert gp.fetch(tab, "AF1QipCCCCCCCCCCCCCCCCoct12", step_s=0.01, wait_s=20)["why"] == "not_signed_in"


def test_every_result_is_dropped_from_the_page(tab):
    _search(tab, words="receipt")
    gp.fetch(tab, "AF1QipCCCCCCCCCCCCCCCCoct12", step_s=0.01, wait_s=20)
    gp.fetch(tab, "AF1QipZZZZZZZZZZZZZZZZnone1", step_s=0.01, wait_s=20)
    assert tab("JSON.stringify(Object.keys(window.__gpRead||{}))") == "[]"


def test_a_tab_that_does_not_answer_is_said():
    assert gp.search(lambda s: None, OCT1, NOV1)["why"] == "no_answer"
    assert gp.fetch(lambda s: None, "AF1QipCCCCCCCCCCCCCCCCoct12")["why"] == "no_answer"


def test_bad_values_are_refused_before_any_script_runs():
    for bad in ("short", "../../etc/passwd", "a" * 200, "AF1Qip with space xxxxxxx"):
        with pytest.raises(ValueError):
            gp.fetch_js("ab12cd34", bad)
    with pytest.raises(ValueError):
        gp.search_js("ab12cd34", dt.datetime(2026, 10, 1), NOV1)       # a time with no zone
    with pytest.raises(ValueError):
        gp.search_js("ab12cd34", NOV1, OCT1)
    with pytest.raises(ValueError):
        gp.search_js("ab12cd34", OCT1, NOV1, words=" ")
    with pytest.raises(ValueError):
        gp.poll_js("NOT-HEX")
    words = 'a "quoted" __GP_TOKEN__ \\ word'
    assert json.dumps(words) in gp.search_js("ab12cd34", OCT1, NOV1, words=words)


def test_nothing_here_presses_types_or_shows_anything():
    src = "".join(open(gp.__file__).read().split('"""')[2:])
    for word in ("click(", "dispatchEvent", "location.href=", "location.assign", "window.open", "focus(", "osascript"):
        assert word not in src, word
