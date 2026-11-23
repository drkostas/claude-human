"""whatsapp_media's scripts, run in node against a stand-in for WhatsApp Web's message store and media
server. The stand-in encrypts a known file the way WhatsApp does (HKDF from a media key, AES-CBC, a
10-byte HMAC), so a fetch has to find the right record, check it, and decrypt it back."""
import json
import shutil
import subprocess
import time

import pytest

from claude_human import whatsapp_media as wm

_PAGE = r"""
globalThis.window = globalThis;
const STORE = new Map();
const FILES = new Map();
let HAS_STORE = true;
function later(r, v) { setTimeout(() => { r.result = v; r.onsuccess && r.onsuccess(); }, 0); }
globalThis.IDBKeyRange = { bound: (l, u) => ({ lower: l, upper: u }) };
function objectStore() {
  return {
    get(k) { const r = {}; later(r, STORE.get(k)); return r; },
    openCursor(range) {
      const r = {}; const keys = [...STORE.keys()].sort().filter(k => k >= range.lower && k <= range.upper);
      let i = 0;
      const step = () => setTimeout(() => {
        if (i >= keys.length) { r.result = null; r.onsuccess(); return; }
        const k = keys[i++]; r.result = { key: k, value: STORE.get(k), continue: step }; r.onsuccess();
      }, 0);
      step(); return r;
    },
  };
}
globalThis.indexedDB = { open() { const r = {}; setTimeout(() => {
  r.result = { objectStoreNames: { contains: n => HAS_STORE && n === 'message' },
               transaction: () => ({ objectStore }), close() {} };
  r.onsuccess(); }, 0); return r; } };
globalThis.fetch = async (url) => {
  const b = FILES.get(url);
  if (!b) return { status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
  return { status: 200, arrayBuffer: async () => b.slice().buffer };
};
const INFO = INFOS;
const b64 = u => { let s = ''; for (const c of u) s += String.fromCharCode(c); return btoa(s); };
const sha = async u => new Uint8Array(await crypto.subtle.digest('SHA-256', u));
async function add(key, o) {
  const plain = new TextEncoder().encode(o.text);
  const mk = crypto.getRandomValues(new Uint8Array(32));
  const base = await crypto.subtle.importKey('raw', mk, 'HKDF', false, ['deriveBits']);
  const k = new Uint8Array(await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32),
    info: new TextEncoder().encode(INFO[o.type]) }, base, 112 * 8));
  const iv = k.slice(0, 16), ck = k.slice(16, 48), mac = k.slice(48, 80);
  const ak = await crypto.subtle.importKey('raw', ck, 'AES-CBC', false, ['encrypt']);
  const enc = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-CBC', iv }, ak, plain));
  const hk = await crypto.subtle.importKey('raw', mac, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signed = new Uint8Array(iv.length + enc.length); signed.set(iv, 0); signed.set(enc, iv.length);
  const tag = new Uint8Array(await crypto.subtle.sign('HMAC', hk, signed)).slice(0, 10);
  const file = new Uint8Array(enc.length + 10); file.set(enc, 0); file.set(tag, enc.length);
  if (o.tamper) file[0] ^= 1;
  const url = 'https://media.example.org/' + key;
  FILES.set(url, file);
  STORE.set(key, { id: key, type: o.type, filename: o.name, size: plain.length, t: o.t,
    mediaKey: o.nokey ? undefined : b64(mk), deprecatedMms3Url: url, filehash: b64(await sha(plain)),
    encFilehash: b64(await sha(file)) });
}
window.add = add;
window.noStore = () => { HAS_STORE = false; return 'ok'; };
window.put = (k, o) => { STORE.set(k, Object.assign({ id: k }, o)); return 'ok'; };
const rl = require('readline').createInterface({ input: process.stdin });
rl.on('line', l => { let out; try { out = eval(JSON.parse(l)); } catch (e) { out = JSON.stringify({ error: String(e) }); }
  process.stdout.write(JSON.stringify(out === undefined ? null : out) + '\n'); });
"""

GROUP = "120363000000000001"
ONE = "15550000001"


class _Tab:
    """A node process holding the stand-in page; calling it runs one script and returns its string."""

    def __init__(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        self.p = subprocess.Popen([node, "-e", _PAGE.replace("INFOS", json.dumps(wm.INFO))],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.calls = []

    def __call__(self, script):
        self.calls.append(script)
        self.p.stdin.write(json.dumps(script) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def add(self, key, **o):
        self(f"window.__set=null;add({json.dumps(key)},{json.dumps(o)}).then(()=>window.__set='done')"
             ".catch(e=>window.__set=String(e));'started'")
        for _ in range(200):
            if self("window.__set") is not None:
                break
            time.sleep(0.01)
        assert self("window.__set") == "done"

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


@pytest.fixture
def tab():
    t = _Tab()
    yield t
    t.close()


def _fetch(tab, chat, **kw):
    return wm.fetch(tab, chat, step_s=0.01, wait_s=20, **kw)


def test_a_group_message_is_fetched_by_its_id_and_decrypted_back(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0AAAAAAAAAAAAAAAA01_15550000002@lid", type="document", name="report.pdf",
            text="the report, page one", t=100)
    got = _fetch(tab, GROUP, message_id="3EB0AAAAAAAAAAAAAAAA01")
    assert got["ok"], got
    assert got["data"] == b"the report, page one"
    assert got["meta"]["filename"] == "report.pdf" and got["meta"]["type"] == "document"
    assert got["meta"]["size"] == 20 and got["meta"]["matches"] == 1
    assert "mediaKey" not in json.dumps(got["meta"]) and "filehash" not in got["meta"]


def test_a_one_to_one_message_is_found_by_its_exact_key(tab):
    tab.add(f"true_{ONE}@c.us_3EB0BBBBBBBBBBBBBBBB02", type="image", name=None, text="jpeg bytes", t=5)
    got = _fetch(tab, ONE, message_id="3EB0BBBBBBBBBBBBBBBB02")
    assert got["ok"] and got["data"] == b"jpeg bytes"


def test_the_same_id_in_another_chat_is_never_taken(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0CCCCCCCCCCCCCCCC03", type="document", name="a.pdf", text="x", t=1)
    got = _fetch(tab, "120363000000000999", message_id="3EB0CCCCCCCCCCCCCCCC03")
    assert not got["ok"] and got["why"] == "no_record"


def test_a_file_name_takes_the_newest_message_in_the_chat_and_says_how_many(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0DDDDDDDDDDDDDDDD04_1@lid", type="document", name="notes.docx", text="old", t=10)
    tab.add(f"false_{GROUP}@g.us_3EB0DDDDDDDDDDDDDDDD05_1@lid", type="document", name="notes.docx", text="new", t=20)
    tab.add(f"false_{ONE}@c.us_3EB0DDDDDDDDDDDDDDDD06", type="document", name="notes.docx", text="other", t=30)
    got = _fetch(tab, GROUP, name="notes.docx")
    assert got["ok"] and got["data"] == b"new" and got["meta"]["matches"] == 2


def test_a_file_read_back_in_many_chunks_is_whole(tab):
    text = "".join(chr(65 + i % 26) for i in range(5000))
    tab.add(f"false_{GROUP}@g.us_3EB0EEEEEEEEEEEEEEEE07", type="document", name="big.txt", text=text, t=1)
    got = wm.fetch(tab, GROUP, message_id="3EB0EEEEEEEEEEEEEEEE07", chunk=777, step_s=0.01, wait_s=20)
    assert got["ok"] and got["data"] == text.encode()
    assert sum("subarray(OFFSET" in c or ".subarray(" in c for c in tab.calls) >= 7, "read in parts"


def test_a_file_that_fails_its_mac_is_not_decrypted(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0FFFFFFFFFFFFFFFF08", type="document", name="m.pdf", text="secret", t=1, tamper=True)
    got = _fetch(tab, GROUP, message_id="3EB0FFFFFFFFFFFFFFFF08")
    assert not got["ok"] and got["why"] == "mac" and "data" not in got


def test_a_message_without_a_media_key_says_so(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0GGGGGGGGGGGGGGGG09", type="document", name="n.pdf", text="n", t=1, nokey=True)
    got = _fetch(tab, GROUP, message_id="3EB0GGGGGGGGGGGGGGGG09")
    assert not got["ok"] and got["why"] == "no_media" and got["meta"]["filename"] == "n.pdf"


def test_a_tab_with_no_message_store_says_so(tab):
    tab("noStore()")
    got = _fetch(tab, GROUP, message_id="3EB0HHHHHHHHHHHHHHHH10")
    assert not got["ok"] and got["why"] == "no_store"


def test_the_file_is_dropped_from_the_page_after_a_fetch(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0IIIIIIIIIIIIIIII11", type="document", name="d.pdf", text="d", t=1)
    assert _fetch(tab, GROUP, message_id="3EB0IIIIIIIIIIIIIIII11")["ok"]
    assert "window.__waMedia" in tab.calls[-1] and "delete" in tab.calls[-1]
    assert tab("JSON.stringify(Object.keys(window.__waMedia))") == "[]"


def test_a_tab_that_does_not_answer_is_said():
    got = wm.fetch(lambda script: None, GROUP, message_id="3EB0JJJJJJJJJJJJJJJJ12", step_s=0, wait_s=0)
    assert not got["ok"] and got["why"] == "no_answer"


def test_values_go_in_as_json_and_bad_ones_are_refused():
    s = wm.start_js("0123abcd", GROUP, name='a "quoted" name.pdf')
    assert json.dumps('a "quoted" name.pdf') in s
    for bad in ({"chat": GROUP + "@g.us", "message_id": "3EB0AAAAAAAAAAAAAAAA01"},
                {"chat": GROUP, "message_id": "3EB0'; alert(1)"},
                {"chat": GROUP},
                {"chat": GROUP, "message_id": "3EB0AAAAAAAAAAAAAAAA01", "name": "x"}):
        with pytest.raises(ValueError):
            wm.start_js("0123abcd", **bad)
    with pytest.raises(ValueError):
        wm.poll_js("not-a-token")


def test_nothing_here_presses_types_or_shows_anything():
    for s in (wm.start_js("0123abcd", GROUP, message_id="3EB0AAAAAAAAAAAAAAAA01"), wm.poll_js("0123abcd"),
              wm.chunk_js("0123abcd", 0), wm.clear_js("0123abcd")):
        for word in ("click", "dispatchEvent", "focus(", "KeyboardEvent", "PointerEvent", "#main"):
            assert word not in s


@pytest.mark.parametrize("raw,want", [
    ("../../etc/passwd", "etcpasswd"),
    ("report.pdf", "report.pdf"),
    (".hidden", "hidden"),
    ("a\x00b\nc.txt", "abc.txt"),
    (None, "attachment"),
    ("x" * 200 + ".pdf", "x" * 116 + ".pdf"),
])
def test_a_file_name_is_made_safe_to_write(raw, want):
    assert wm.safe_name(raw) == want


def test_a_file_name_holding_every_marker_word_stays_whole_and_the_script_runs(tab):
    import re
    markers = sorted(set(re.findall(r"__WA_[A-Z]+__", wm._START + wm.POLL + wm.CHUNK_JS + wm.CLEAR)))
    assert len(markers) >= 10
    name = "Invoice " + " ".join(markers) + " TOKEN CHATID MSGID FNAME MAXB SERVERS INFOS MHOST OFFSET SIZE.pdf"
    script = wm.start_js("0123abcd", GROUP, name=name)
    assert json.dumps(name) in script, "the name is carried as it was given"
    tab.add(f"false_{GROUP}@g.us_3EB0KKKKKKKKKKKKKKKK13_1@lid", type="document", name=name, text="kept whole", t=1)
    got = _fetch(tab, GROUP, name=name)
    assert got["ok"] and got["data"] == b"kept whole" and got["meta"]["filename"] == name


def test_a_message_is_found_by_the_time_it_was_sent_and_its_id_is_said(tab):
    # a photo has no file name, and a reader may know only the minute it was sent
    tab.add(f"false_{GROUP}@g.us_3EB0LLLLLLLLLLLLLLLL14_15550000002@lid", type="image", name=None,
            text="photo bytes", t=1000)
    tab("put(" + json.dumps(f"false_{GROUP}@g.us_3EB0LLLLLLLLLLLLLLLL15_1@lid") + ",{type:'chat',t:1010})")
    tab.add(f"false_{ONE}@c.us_3EB0LLLLLLLLLLLLLLLL16", type="image", name=None, text="another chat", t=1005)
    got = _fetch(tab, GROUP, sent=(990, 1050))
    assert got["ok"], got
    assert got["data"] == b"photo bytes" and got["meta"]["id"] == "3EB0LLLLLLLLLLLLLLLL14"
    assert got["meta"]["matches"] == 1 and got["meta"]["sent_at"] == 1000


def test_two_files_sent_in_the_window_take_neither_and_name_each(tab):
    tab.add(f"true_{ONE}@c.us_3EB0MMMMMMMMMMMMMMMM17", type="image", name=None, text="first", t=2030)
    tab.add(f"true_{ONE}@c.us_3EB0MMMMMMMMMMMMMMMM18", type="document", name="b.pdf", text="second", t=2000)
    got = _fetch(tab, ONE, sent=(1990, 2050))
    assert not got["ok"] and got["why"] == "ambiguous" and "data" not in got
    assert got["candidates"] == [
        {"id": "3EB0MMMMMMMMMMMMMMMM18", "type": "document", "filename": "b.pdf", "sent_at": 2000},
        {"id": "3EB0MMMMMMMMMMMMMMMM17", "type": "image", "filename": None, "sent_at": 2030}]
    again = _fetch(tab, ONE, message_id=got["candidates"][1]["id"])
    assert again["ok"] and again["data"] == b"first" and again["meta"]["id"] == "3EB0MMMMMMMMMMMMMMMM17"


def test_no_file_sent_in_the_window_is_no_record(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0NNNNNNNNNNNNNNNN19", type="image", name=None, text="later", t=5000)
    got = _fetch(tab, GROUP, sent=(4000, 4999))
    assert not got["ok"] and got["why"] == "no_record"


def test_a_file_name_fetch_says_the_message_id_too(tab):
    tab.add(f"false_{GROUP}@g.us_3EB0OOOOOOOOOOOOOOOO20_1@lid", type="document", name="o.pdf", text="o", t=1)
    got = _fetch(tab, GROUP, name="o.pdf")
    assert got["ok"] and got["meta"]["id"] == "3EB0OOOOOOOOOOOOOOOO20"


@pytest.mark.parametrize("sent", [(10, 5), (5, 5), ("a", 9), (-1, 9), (0, 2 * 86400), (1, 2, 3)])
def test_a_bad_time_window_is_refused(sent):
    with pytest.raises(ValueError):
        wm.start_js("0123abcd", GROUP, sent=sent)
    with pytest.raises(ValueError):
        wm.start_js("0123abcd", GROUP, sent=(1, 9), name="x")
