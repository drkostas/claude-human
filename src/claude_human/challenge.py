"""Whether a page shows a robot check that a person can see, as JavaScript to run in the page.

A sign-in page often carries a challenge frame that asks nothing of anybody. reCAPTCHA v3 puts its anchor
iframe on every page of a site that uses it, inside `.grecaptcha-badge`, often 0 by 0 and sometimes as a
small badge in a corner. hCaptcha and Turnstile load frames that are hidden, see-through or cut away by a
box around them until a check is needed. An automation that treats any such frame as a robot check hands a
person a step that does not exist.

`present_js` answers true only for a challenge that is drawn: visible, not see-through, not inside a hidden
box, and with at least `MIN_PX` pixels each way left inside the window and inside every box around it that
cuts off its overflow. A visible Turnstile widget is about 300 by 65 pixels and hCaptcha's box 303 by 78,
so 20 pixels leaves room for both and none for a frame that only loads a script.

    from claude_human import challenge
    run_js(challenge.present_js())                       # any drawn challenge frame of a known vendor
    run_js(challenge.present_js(".g-recaptcha,iframe[src*=hcaptcha]"))   # the elements a recipe names

`drawn_js` is the test on one element, for a script that reads more of the page at once.

A challenge inside a frame that is itself inside another frame has no address the top page can read, so
`says_robot_check` reads the words instead: a step's name, or a line read from the window.

    challenge.says_robot_check("I'm not a robot")         # True
    challenge.says_robot_check("Save and Continue")       # False
"""
from __future__ import annotations

import json
import re

#: the frames of the challenge vendors this knows, matched against an iframe's address
VENDORS = r"challenges[.]cloudflare|hcaptcha|recaptcha|turnstile|arkose|funcaptcha"
#: the fewest pixels each way a drawn challenge has inside the window
MIN_PX = 20
#: reCAPTCHA v3's badge, which holds its frame on every page and is no step of anybody's
V3_BADGE = ".grecaptcha-badge"
#: the words a robot check shows a person, matched against text with its case and spacing set aside. A challenge
#: can sit in a frame inside another frame, where a script in the top page sees no address to match against
#: VENDORS, so the words a step or a button says are checked as well. The pattern uses no lookbehind, so the same
#: string works as a JavaScript RegExp with the "i" flag.
WORDS = (r"\bnot a robot\b"
         r"|\bi am (?:a )?human\b"
         r"|\b(?:verify|verifying|prove|proving|confirm|confirming) (?:that )?"
         r"(?:you(?:'|\u2019| a)re|i am|i(?:'|\u2019)m) (?:a )?human\b"
         r"|\bare you (?:a )?(?:robot|human)\b"
         r"|\bhuman verification\b"
         r"|\bpress (?:and|&) hold\b"
         r"|\bchecking your browser before accessing\b")


def says_robot_check(text: str | None) -> bool:
    """True when `text` says what a robot check says to a person ("I'm not a robot", "Verifying you are human",
    "Press and hold", "Checking your browser before accessing"), whatever its case, spacing or kind of apostrophe. For a step's words or a line read from a
    window, where no frame address is there to check."""
    words = re.sub(r"\s+", " ", text or "").strip()
    return bool(words) and re.search(WORDS, words, re.IGNORECASE) is not None


def drawn_js(min_px: int = MIN_PX) -> str:
    """A JavaScript function expression, `function(e){...}`, true when the element is drawn: visible, no box
    around it hidden or see-through (opacity under 0.1), and at least `min_px` pixels each way of it inside
    the window and inside every box around it that cuts off its overflow."""
    least = str(int(min_px))
    return ("function(f){var r=f.getBoundingClientRect(),s=getComputedStyle(f),"
            "x0=Math.max(r.left,0),y0=Math.max(r.top,0),x1=Math.min(r.right,window.innerWidth),"
            "y1=Math.min(r.bottom,window.innerHeight);if(s.visibility!=='visible')return false;"
            "if(f.checkVisibility&&!f.checkVisibility({opacityProperty:true,visibilityProperty:true}))return false;"
            "for(var e=f;e&&e.nodeType===1;e=e.parentElement){var st=getComputedStyle(e);"
            "if(st.display==='none'||parseFloat(st.opacity)<0.1)return false;"
            "if(e!==f&&e!==document.body&&e!==document.documentElement&&"
            "(st.overflowX!=='visible'||st.overflowY!=='visible')){var p=e.getBoundingClientRect();"
            "x0=Math.max(x0,p.left);y0=Math.max(y0,p.top);x1=Math.min(x1,p.right);y1=Math.min(y1,p.bottom)}}"
            "return x1-x0>=" + least + "&&y1-y0>=" + least + "}")


def present_js(selector: str | None = None, min_px: int = MIN_PX) -> str:
    """A JavaScript expression, true when a drawn robot check is on the page. With no `selector` it looks at
    every iframe whose address names a vendor in VENDORS; with one, at the elements the selector matches.
    Anything inside reCAPTCHA v3's badge is passed over, drawn or not."""
    pick = (f"document.querySelectorAll({json.dumps(selector)})" if selector
            else "document.querySelectorAll('iframe')")
    vendor = "true" if selector else f"/{VENDORS}/i.test(e.src||'')"
    return ("(function(){var drawn=" + drawn_js(min_px) + ";var l=" + pick + ";"
            "for(var i=0;i<l.length;i++){var e=l[i];if(!(" + vendor + "))continue;"
            f"if(e.closest&&e.closest({json.dumps(V3_BADGE)}))continue;"
            "if(drawn(e))return true;}return false;})()")
