"""challenge's scripts, run in node against small stand-in pages: each element has a box, a computed style, a
parent and an address, which is all the scripts read."""
import json
import shutil
import subprocess

import pytest

from claude_human import challenge

_DOM = r"""
const W = 1200, H = 800;
function box(o) { return { left: o.x || 0, top: o.y || 0, right: (o.x || 0) + o.w, bottom: (o.y || 0) + o.h, width: o.w, height: o.h }; }
function el(o, parent) {
  const e = { nodeType: 1, src: o.src || '', parentElement: parent || null, badge: !!o.badge, picked: !!o.picked,
    tag: o.tag || 'iframe', rect: box(o),
    st: { visibility: o.vis || 'visible', display: o.disp || 'block', opacity: o.op === undefined ? '1' : String(o.op),
          overflowX: o.clip ? 'hidden' : 'visible', overflowY: o.clip ? 'hidden' : 'visible' } };
  e.getBoundingClientRect = () => e.rect;
  e.closest = s => { for (let p = e; p; p = p.parentElement) if (s === '.grecaptcha-badge' && p.badge) return p; return null; };
  return e;
}
globalThis.window = globalThis; window.innerWidth = W; window.innerHeight = H;
globalThis.getComputedStyle = e => e.st;
const body = el({ tag: 'body', w: W, h: H });
function page(list) {
  const all = list.map(o => {
    const parent = o.in ? el(o.in, body) : body;
    if (o.in && o.in.badge) parent.badge = true;
    return el(o, parent);
  });
  globalThis.document = { body, documentElement: body,
    querySelectorAll: s => all.filter(e => s === 'iframe' ? e.tag === 'iframe' : e.picked) };
}
const rl = require('readline').createInterface({ input: process.stdin });
rl.on('line', l => { const [list, code] = JSON.parse(l); page(list); let out;
  try { out = eval(code); } catch (err) { out = 'error: ' + err; }
  process.stdout.write(JSON.stringify(out) + '\n'); });
"""

RECAPTCHA = "https://www.google.com/recaptcha/api2/anchor?k=x"
TURNSTILE = "https://challenges.cloudflare.com/turnstile/v0/x"
HCAPTCHA = "https://newassets.hcaptcha.com/captcha/v1/x"


@pytest.fixture(scope="module")
def run():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    p = subprocess.Popen([node, "-e", _DOM], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def ask(elements, code=None):
        p.stdin.write(json.dumps([elements, code or challenge.present_js()]) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())
    yield ask
    p.stdin.close()
    p.wait(timeout=10)


def test_a_drawn_challenge_is_a_robot_check(run):
    assert run([{"src": TURNSTILE, "w": 300, "h": 65, "y": 200}]) is True
    assert run([{"src": HCAPTCHA, "w": 303, "h": 78, "y": 200}]) is True
    assert run([{"src": RECAPTCHA, "w": 304, "h": 78, "y": 200}]) is True


def test_the_v3_badge_is_never_one_drawn_or_not(run):
    assert run([{"src": RECAPTCHA, "w": 0, "h": 0, "in": {"badge": True, "w": 0, "h": 0}}]) is False
    assert run([{"src": RECAPTCHA, "w": 256, "h": 60, "x": 940, "y": 730,
                 "in": {"badge": True, "w": 256, "h": 60, "x": 940, "y": 730}}]) is False


def test_a_frame_nobody_can_see_is_none(run):
    assert run([{"src": RECAPTCHA, "w": 400, "h": 580, "y": -10000}]) is False, "a challenge waiting off the screen"
    assert run([{"src": HCAPTCHA, "w": 300, "h": 150, "vis": "hidden"}]) is False
    assert run([{"src": HCAPTCHA, "w": 300, "h": 150, "in": {"w": 300, "h": 150, "disp": "none"}}]) is False
    assert run([{"src": HCAPTCHA, "w": 300, "h": 150, "in": {"w": 300, "h": 150, "op": 0}}]) is False
    assert run([{"src": HCAPTCHA, "w": 300, "h": 150, "in": {"w": 300, "h": 0, "clip": True}}]) is False, \
        "a frame cut away by a box of no height"
    assert run([{"src": HCAPTCHA, "w": 19, "h": 300}]) is False, "narrower than MIN_PX"


def test_only_a_vendor_frame_counts_without_a_selector(run):
    assert run([{"src": "https://example.org/map", "w": 600, "h": 400}]) is False
    assert run([]) is False


def test_a_selector_names_the_elements_to_look_at(run):
    sel = ".g-recaptcha,iframe[src*=hcaptcha]"
    code = challenge.present_js(sel)
    assert json.dumps(sel) in code
    assert run([{"tag": "div", "picked": True, "w": 304, "h": 78, "y": 300}], code) is True
    assert run([{"tag": "div", "picked": True, "w": 304, "h": 78, "y": 300, "in": {"badge": True, "w": 304, "h": 78}}],
               code) is False
    assert run([{"tag": "div", "picked": False, "w": 304, "h": 78}], code) is False


def test_min_px_and_drawn_js_are_the_same_test(run):
    assert run([{"src": TURNSTILE, "w": 40, "h": 40}], challenge.present_js(min_px=50)) is False
    assert run([{"src": TURNSTILE, "w": 40, "h": 40}], challenge.present_js(min_px=30)) is True
    one = "(" + challenge.drawn_js() + ")(document.querySelectorAll('iframe')[0])"
    assert run([{"src": "https://example.org/any", "w": 300, "h": 65}], one) is True
    assert run([{"src": "https://example.org/any", "w": 300, "h": 65, "op": 0.05}], one) is False


def test_the_words_of_a_robot_check_are_one():
    for words in ("I'm not a robot", "I’m not a robot", "i am not a robot", "  I'M   NOT A ROBOT ",
                  "Verify you are human", "Verify that you're human", "Prove you are a human",
                  "Confirm you are human", "Are you a robot?", "I am human", "Human verification",
                  "Press and hold", "Press & Hold the button"):
        assert challenge.says_robot_check(words), words


def test_other_words_are_not_one():
    for words in ("Save and Continue", "Sign in", "Submit", "Robotics team", "Human resources",
                  "Press the button and hold on", "Not now", "", None):
        assert not challenge.says_robot_check(words), words


def test_the_words_pattern_works_as_a_javascript_regexp(run):
    code = ("(function(){var r=new RegExp(" + json.dumps(challenge.WORDS) + ",'i');"
            "return [r.test(\"I'm not a robot\"),r.test('Verify you are human'),r.test('Save and Continue')]})()")
    assert run([], code) == [True, True, False]
