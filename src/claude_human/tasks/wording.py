"""Whether a text asks a person to do something by hand.

An assistant that needs a person should ask for a decision or a permission, then act itself. A
question or a note that tells the person to open a page, sign in, type a password or tap a button
hands the work back to them, and on a phone it may be work they cannot do at all. ``by_hand`` finds
such a sentence so a tool can refuse the text before it is sent, and say how to ask instead.

    from claude_human.tasks import by_hand, hand_ask
    by_hand("Open the settings page and tap Allow.")     # "Open the settings page and tap Allow."
    by_hand("Which account should I use?")               # None
    hand_ask('Please press "Decline" now.')              # HandAsk(sentence='Please press "Decline" now.', verb='press')

A sentence counts when it starts with one of the verbs (after "please", "then", "now", "first", a
list number or a bullet, and after a place set off by a comma, as in "In the app on your phone, open
the page"), or when the verb follows "you need to", "you should", "you must", "you
have to", "can you", "could you" or "please". Text in quotation marks is a quote, never an ask, so a
message that reports a refusal word for word passes. A sentence where someone else acts ("Should I
open a pull request?") passes too, and so does "open" used as an adjective ("Open items, each with its
date").

``hand_ask`` also gives the verb that made the sentence an instruction. A refusal that names it tells
the writer what to change. "Please press Decline" is refused for "press", while "Decline is the right
answer" passes, so a refusal that only quoted the sentence left the writer to guess whether the
button's name or the verb was the problem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: what a person is asked to do by hand, as the words of an instruction
HAND_VERBS = ("open", "sign in", "sign into", "log in", "log into", "install", "plug", "unplug", "go to",
              "click", "press", "tap", "type", "enter your", "paste", "restart", "reboot", "unlock", "run",
              # gestures on a touch screen
              "pinch", "spread your fingers", "swipe", "scroll", "drag", "long-press", "long press")
#: a place set off by a comma before an instruction ("In the app on your phone, open ...")
_PLACE = r"(?:(?:in|on|at|from|inside|within)\s+[^,.!?\n]{1,80},\s*)?"
#: words that may come before the verb at the start of an instruction
_LEAD = r"(?:(?:please|then|now|first|next|and|also|just)\s+|(?:\d+[.)]|[-*•])\s+)*"
#: words that turn the verb that follows into an ask of the reader
_ASKING = r"(?:you\s+(?:need|have)\s+to|you\s+(?:should|must|can)|can\s+you|could\s+you|would\s+you|please)\s+"
#: nouns that make "open" an adjective, so "Open items, each with its date" names a list and asks nothing
_OPEN_ADJECTIVE = r"open\s+(?:items?|questions?|tasks?|issues?|problems?|points?|tickets?|bugs?|threads?|work|prs?|pull\s+requests?)\b"
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|`[^`]*`")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class HandAsk:
    """A sentence that tells the reader to act by hand, as written (quotations kept), and the verb in
    it that made it an instruction, as written with its spaces made single ("press", "Sign in")."""
    sentence: str
    verb: str


def _pattern(verbs: tuple[str, ...]) -> re.Pattern:
    words = "|".join(sorted((re.escape(v).replace(r"\ ", r"\s+") for v in verbs), key=len, reverse=True))
    verb = rf"(?!{_OPEN_ADJECTIVE})(?:{words})\b"
    return re.compile(rf"(?:^\s*{_PLACE}{_LEAD}|\b{_ASKING})(?P<verb>{verb})", re.I)


def _unquoted(text: str) -> tuple[str, list[int], list[int]]:
    """`text` with each quotation made one space, and for each of its characters where it starts and
    ends in `text`, so a sentence found in it can be given back as it was written."""
    parts, starts, ends, at = [], [], [], 0
    for m in _QUOTED.finditer(text):
        parts.append(text[at:m.start()])
        starts.extend(range(at, m.start()))
        ends.extend(range(at + 1, m.start() + 1))
        parts.append(" ")
        starts.append(m.start())
        ends.append(m.end())
        at = m.end()
    parts.append(text[at:])
    starts.extend(range(at, len(text)))
    ends.extend(range(at + 1, len(text) + 1))
    return "".join(parts), starts, ends


def hand_ask(text: str | None, verbs: tuple[str, ...] = HAND_VERBS) -> HandAsk | None:
    """The first sentence of `text` that tells the reader to do something by hand, with the verb that
    made it one, or None."""
    pattern = _pattern(verbs)
    plain, starts, ends = _unquoted(text or "")
    at = 0
    for gap in [*_SENTENCES.finditer(plain), None]:
        end = gap.start() if gap else len(plain)
        found = pattern.search(plain[at:end])
        # a bare verb that ends in a question mark ("Open the PR?") is the asker offering its own next act, and a
        # question to the reader about it is not an instruction. "Could you press Decline?" still is one.
        if found and plain[at:end].rstrip().endswith("?") and not re.match(_ASKING, found.group(0).lstrip(), re.I):
            found = None
        if found and end > at:
            return HandAsk(sentence=(text or "")[starts[at]:ends[end - 1]].strip(),
                           verb=" ".join(found.group("verb").split()))
        # the next sentence starts at a quotation inside the gap, so a sentence that opens with one keeps it
        at = next((k for k in range(gap.start(), gap.end()) if ends[k] - starts[k] > 1), gap.end()) if gap else end
    return None


def by_hand(text: str | None, verbs: tuple[str, ...] = HAND_VERBS) -> str | None:
    """The first sentence of `text` that tells the reader to do something by hand, as written, or None."""
    found = hand_ask(text, verbs)
    return found.sentence if found else None
