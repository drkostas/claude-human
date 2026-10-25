"""Whether a text asks a person to do something by hand.

An assistant that needs a person should ask for a decision or a permission, then act itself. A
question or a note that tells the person to open a page, sign in, type a password or tap a button
hands the work back to them, and on a phone it may be work they cannot do at all. ``by_hand`` finds
such a sentence so a tool can refuse the text before it is sent, and say how to ask instead.

    from claude_human.tasks import by_hand
    by_hand("Open the settings page and tap Allow.")     # "Open the settings page and tap Allow."
    by_hand("Which account should I use?")               # None

A sentence counts when it starts with one of the verbs (after "please", "then", "now", "first", a
list number or a bullet), or when the verb follows "you need to", "you should", "you must", "you
have to", "can you", "could you" or "please". Text in quotation marks is a quote, never an ask, so a
message that reports a refusal word for word passes. A sentence where someone else acts ("Should I
open a pull request?") passes too.
"""
from __future__ import annotations

import re

#: what a person is asked to do by hand, as the words of an instruction
HAND_VERBS = ("open", "sign in", "sign into", "log in", "log into", "install", "plug", "unplug", "go to",
              "click", "press", "tap", "type", "enter your", "paste", "restart", "reboot", "unlock", "run")
#: words that may come before the verb at the start of an instruction
_LEAD = r"(?:(?:please|then|now|first|next|and|also|just)\s+|(?:\d+[.)]|[-*•])\s+)*"
#: words that turn the verb that follows into an ask of the reader
_ASKING = r"(?:you\s+(?:need|have)\s+to|you\s+(?:should|must|can)|can\s+you|could\s+you|would\s+you|please)\s+"
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|`[^`]*`")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


def _pattern(verbs: tuple[str, ...]) -> re.Pattern:
    words = "|".join(sorted((re.escape(v).replace(r"\ ", r"\s+") for v in verbs), key=len, reverse=True))
    return re.compile(rf"^\s*{_LEAD}(?:{words})\b|\b{_ASKING}(?:{words})\b", re.I)


def by_hand(text: str | None, verbs: tuple[str, ...] = HAND_VERBS) -> str | None:
    """The first sentence of `text` that tells the reader to do something by hand, or None."""
    pattern = _pattern(verbs)
    for sentence in _SENTENCES.split(_QUOTED.sub(" ", text or "")):
        if pattern.search(sentence):
            return sentence.strip()
    return None
