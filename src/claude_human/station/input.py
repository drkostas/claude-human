"""Clicks, scrolls and keys from a phone, posted on the Mac as CGEvents.

Coordinates arrive normalised (0 to 1 inside the window, or inside the main display when no window
is named). The phone does not know the Mac's resolution, and the window can move between frames, so
a client that sent pixels would click where the window used to be.

Every refusal is returned, never raised and never hidden: ``{"ok": False, ...}`` for a locked
session, for a system dialog that holds focus, and for a named window that is not on the visible
desktop. A caller that reads only "no exception" would report success for input the Mac dropped.

Quartz is imported when the first event is posted, so this module loads on any platform and its
logic can be tested with a stand-in for Quartz.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

LOCKED_DETAIL = "The Mac is locked, so it will not accept clicks or typing."
BLOCKED_DETAIL = ("A macOS permission dialog is open on the Mac and is taking every click. "
                  "Answer it on the Mac, then try again.")
FOCUS_DETAIL = ("That window is not in front on the Mac, so typing would go to another window. "
                "Tap it first, then type.")

#: The input types that only move the pointer. They are allowed while the Mac is locked or a dialog
#: is up, because they cannot press anything.
HARMLESS = frozenset({"move"})

#: Keys the station is willing to type for the ``spotlight`` act: Command and Space, nothing else.
SPACE = 49
V_KEY = 9


class QuartzInput:
    """The input sink the server uses by default.

    ``screen`` answers ``screen_locked()``, ``blocked_by()``, ``resolve(app, wid)``,
    ``window_rect(wid)`` and ``front_window()``. ``claude_human.screenshot`` is the default. ``post`` sends one event (the
    default posts it to the HID event tap), and ``quartz`` is the Quartz module or a stand-in."""

    def __init__(self, screen: Any = None, *, post: Optional[Callable[[Any], None]] = None,
                 quartz: Any = None, pasteboard: Any = None,
                 locked_detail: str = LOCKED_DETAIL, blocked_detail: str = BLOCKED_DETAIL,
                 sleep: Callable[[float], None] = time.sleep):
        if screen is None:
            from .. import screenshot as screen  # noqa: PLC0415
        self.screen = screen
        self._quartz = quartz
        self._post_fn = post
        self._pasteboard = pasteboard
        self.locked_detail = locked_detail
        self.blocked_detail = blocked_detail
        self.sleep = sleep

    # ------------------------------------------------------------------ plumbing

    @property
    def q(self):
        if self._quartz is None:
            import Quartz  # noqa: PLC0415 - macOS only
            self._quartz = Quartz
        return self._quartz

    def post(self, ev) -> None:
        if self._post_fn is not None:
            self._post_fn(ev)
        else:
            self.q.CGEventPost(self.q.kCGHIDEventTap, ev)

    def buttons(self, name: str):
        q = self.q
        table = {
            "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp,
                     q.kCGEventLeftMouseDragged, q.kCGMouseButtonLeft),
            "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp,
                      q.kCGEventRightMouseDragged, q.kCGMouseButtonRight),
        }
        return table.get(name, table["left"])

    def can_act(self) -> bool:
        """Whether this process may post clicks, as macOS answers it (the Accessibility grant).

        Ask rather than test. A test click lands at a point, so a window behind another never rises
        and a working station looks broken."""
        try:
            import ApplicationServices  # noqa: PLC0415
            return bool(ApplicationServices.AXIsProcessTrusted())
        except Exception:
            return False

    def to_screen(self, wid: Optional[int], nx: float, ny: float):
        """Normalised client coordinates to a point on the desktop, or None when the window is gone."""
        q = self.q
        if wid:
            r = self.screen.window_rect(wid)
            if r is None:
                return None
            return q.CGPointMake(r.origin.x + nx * r.size.width, r.origin.y + ny * r.size.height)
        disp = q.CGDisplayBounds(q.CGMainDisplayID())
        return q.CGPointMake(nx * disp.size.width, ny * disp.size.height)

    def _keypress(self, code: int, flags: int = 0) -> None:
        q = self.q
        for is_down in (True, False):
            ev = q.CGEventCreateKeyboardEvent(None, code, is_down)
            if flags:
                q.CGEventSetFlags(ev, flags)
            self.post(ev)

    def _paste(self, text: str) -> dict:
        """Type by pasting. Synthesised characters do not reach SwiftUI text fields (they ride on
        keycode 0 and are dropped while the call reports success), but Command+V is a real keycode
        and lands. The clipboard the person had is put back afterwards."""
        try:
            if self._pasteboard is not None:
                pb, kind = self._pasteboard
            else:
                from AppKit import NSPasteboard, NSStringPboardType  # noqa: PLC0415
                pb, kind = NSPasteboard.generalPasteboard(), NSStringPboardType
            prior = pb.stringForType_(kind)
            pb.clearContents()
            pb.setString_forType_(text, kind)
            self.sleep(0.05)
            self._keypress(V_KEY, self.q.kCGEventFlagMaskCommand)
            self.sleep(0.25)
            if prior is not None:
                pb.clearContents()
                pb.setString_forType_(prior, kind)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"could not type: {e}"}

    # ------------------------------------------------------------------ the one entry point

    def do_input(self, msg: dict) -> dict:
        kind = msg.get("type")
        if kind not in HARMLESS and self.screen.screen_locked():
            return {"ok": False, "locked": True, "detail": self.locked_detail}
        if kind not in HARMLESS and self.screen.blocked_by():
            return {"ok": False, "blocked": True, "detail": self.blocked_detail}
        # A request that names a window and cannot find it is refused. It must never fall back to
        # the whole display: the normalised point would then land at the same place on whatever the
        # person at the Mac is looking at, as a real click.
        asked_for_window = bool(msg.get("app") or msg.get("window"))
        wid = self.screen.resolve(msg.get("app"), int(msg["window"]) if msg.get("window") else None)
        if asked_for_window and wid is None:
            return {"ok": False, "detail": "that window is not on the visible desktop"}
        q = self.q
        down, up, drag, which = self.buttons(str(msg.get("button", "left")))

        if kind in ("move", "down", "up", "click", "dblclick", "drag"):
            pt = self.to_screen(wid, float(msg.get("x", 0)), float(msg.get("y", 0)))
            if pt is None:
                return {"ok": False, "detail": "that window is gone"}
            if kind == "move":
                self.post(q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, pt, which))
            elif kind == "drag":
                self.post(q.CGEventCreateMouseEvent(None, drag, pt, which))
            elif kind == "down":
                self.post(q.CGEventCreateMouseEvent(None, down, pt, which))
            elif kind == "up":
                self.post(q.CGEventCreateMouseEvent(None, up, pt, which))
            else:
                # The click count is what makes two clicks a double click. Without it macOS sees two
                # separate clicks and text is never selected.
                for i in range(2 if kind == "dblclick" else 1):
                    for edge in (down, up):
                        e = q.CGEventCreateMouseEvent(None, edge, pt, which)
                        q.CGEventSetIntegerValueField(e, q.kCGMouseEventClickState, i + 1)
                        self.post(e)
            return {"ok": True}

        if kind == "scroll":
            e = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitPixel, 2,
                                                int(msg.get("dy", 0)), int(msg.get("dx", 0)))
            if wid is not None:
                # ⚠️ A SCROLL GOES TO THE WINDOW UNDER THE POINTER, wherever the person at the Mac
                # left it. For a named window the pointer is moved inside it first and the scroll
                # is placed there, so it never scrolls the window someone else is reading.
                pt = self.to_screen(wid, float(msg.get("x", 0.5)), float(msg.get("y", 0.5)))
                if pt is None:
                    return {"ok": False, "detail": "that window is gone"}
                self.post(q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, pt, which))
                q.CGEventSetLocation(e, pt)
            self.post(e)
            return {"ok": True}

        if kind in ("text", "key") and wid is not None:
            # ⛔ TEXT AND KEYS GO TO WHATEVER WINDOW IS IN FRONT, NOT TO THE ONE NAMED. Typed into
            # a station whose window was behind another, they landed in the person's own window.
            # A tap on the window brings it forward, so the answer is "tap it first". A screen that
            # cannot say what is in front refuses rather than guesses.
            front = getattr(self.screen, "front_window", None)
            if not callable(front) or front() != wid:
                return {"ok": False, "detail": FOCUS_DETAIL}

        if kind == "text":
            return self._paste(str(msg.get("text", ""))[:2000])

        if kind == "spotlight":
            # One named act. The page says "spotlight" and the station decides it is Command+Space,
            # so no request body can turn it into another key combination.
            self._keypress(SPACE, q.kCGEventFlagMaskCommand)
            return {"ok": True}

        if kind == "key":
            self._keypress(int(msg.get("code", 0)), int(msg.get("flags", 0)))
            return {"ok": True}

        return {"ok": False, "detail": f"unknown input {kind!r}"}
