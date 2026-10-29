"""Read the text lines in a picture, with where each one is, through macOS's Vision framework.

`lines(path)` gives each recognised line as {"text", "x", "y", "w", "h"}, the box as fractions of the
picture with its origin at the top left (Vision's own origin is the bottom left), in reading order:
top to bottom, then left to right. A caller that has a window's picture can then find a label by
its words and press the middle of its box through whatever clicks in that window.

`find(found, text)` and `in_band(found, top, bottom)` pick lines out of that list, and are pure.

Reading a picture needs no permission. Taking one of a window does (Screen Recording), which is
why a caller usually gets the picture from a process that holds the grant, such as the station.
"""
from __future__ import annotations

import os
from typing import Iterable

#: lines whose tops differ by less than this share of the picture count as one row in reading order
ROW = 0.01


def _order(found: list[dict]) -> list[dict]:
    return sorted(found, key=lambda d: (round(d["y"] / ROW), d["x"]))


def lines(image_path: str | os.PathLike, *, accurate: bool = False) -> list[dict] | None:
    """Each text line in the picture at `image_path`, in reading order, or None when Vision cannot
    read it (not on macOS, or not a picture). `accurate` asks Vision for its slower, more careful
    recognition."""
    try:
        import Vision
        from Foundation import NSURL
    except ImportError:
        return None
    try:
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(0 if accurate else 1)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(str(image_path)), None)
        ok, _ = handler.performRequests_error_([req], None)
        if not ok:
            return None
        found = []
        for r in req.results() or []:
            best = r.topCandidates_(1)
            if not best:
                continue
            b = r.boundingBox()
            found.append({"text": str(best[0].string()), "x": float(b.origin.x),
                          "y": float(1.0 - b.origin.y - b.size.height),
                          "w": float(b.size.width), "h": float(b.size.height)})
        return _order(found)
    except Exception:   # noqa: BLE001 - a picture Vision chokes on reads as unreadable
        return None


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def find(found: Iterable[dict], text: str, *, left: float = 0.0, right: float = 1.0) -> list[dict]:
    """Pure. The lines whose words are exactly `text` (spaces and case aside) and whose middle lies
    between `left` and `right` across the picture, in reading order."""
    return [d for d in found if _same(d["text"], text) and left <= d["x"] + d["w"] / 2 <= right]


def in_band(found: Iterable[dict], top: float, bottom: float, *, left: float = 0.0,
            right: float = 1.0) -> list[dict]:
    """Pure. The lines whose middle lies inside the band from `top` to `bottom` down the picture and
    from `left` to `right` across it, in reading order."""
    return [d for d in found if top <= d["y"] + d["h"] / 2 <= bottom and left <= d["x"] + d["w"] / 2 <= right]


def middle(line: dict) -> tuple[float, float]:
    """Pure. The middle of a line's box, as fractions of the picture."""
    return line["x"] + line["w"] / 2, line["y"] + line["h"] / 2
