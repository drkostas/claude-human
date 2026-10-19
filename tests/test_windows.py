"""One window at a time: moving it to a desktop, closing it, sizing it, and finding a browser's
window among yabai's. Every act is read back; an answer that could not be read is unknown."""
import json

from claude_human import brave, desktops, screenshot


class FakeWindows:
    """yabai's windows and spaces, and what each window command does to them."""

    def __init__(self):
        self.spaces = [{"uuid": "U1", "index": 1, "label": "one", "is-visible": True},
                       {"uuid": "U2", "index": 2, "label": "two", "is-visible": False}]
        self.wins = [{"id": 7, "space": 1, "app": "Term", "title": "t",
                      "frame": {"x": 0, "y": 0, "w": 500, "h": 400}}]
        self.locked = False
        self.lost = set()                             # ids listed but never located

    def run(self, cmd, **k):
        class R:
            returncode, stdout, stderr = 0, "", ""
        r, args = R(), cmd[2:]
        if args[:2] == ["query", "--spaces"]:
            r.stdout = json.dumps(self.spaces)
        elif args[:2] == ["query", "--windows"] and "--window" in args:
            w = next((w for w in self.wins if str(w["id"]) == args[3]), None)
            if self.locked or w is None or w["id"] in self.lost:
                r.returncode, r.stderr = 1, f"could not locate window with the specified id '{args[3]}'."
            else:
                r.stdout = json.dumps(w)
        elif args[:2] == ["query", "--windows"]:
            r.stdout = json.dumps(self.wins)
        elif args[:1] == ["window"]:
            w = next((w for w in self.wins if str(w["id"]) == args[1]), None)
            if self.locked or w is None or w["id"] in self.lost:
                r.returncode, r.stderr = 1, "could not locate the window to act on!"
            elif args[2] == "--space":
                w["space"] = next(s["index"] for s in self.spaces if s["label"] == args[3])
            elif args[2] == "--move":
                _, x, y = args[3].split(":")
                w["frame"].update(x=float(x), y=float(y))
            elif args[2] == "--resize":
                _, ww, hh = args[3].split(":")
                w["frame"].update(w=float(ww), h=float(hh))
        return r


def _fake(monkeypatch):
    f = FakeWindows()
    monkeypatch.setattr(desktops.subprocess, "run", f.run)
    return f


def test_a_move_succeeds_only_when_the_window_is_read_back_there(monkeypatch):
    f = _fake(monkeypatch)
    assert desktops.move_window(7, "U2") == ("success", "")
    assert desktops.space_of_window(7) == "U2"


def test_a_failed_move_carries_yabais_own_words(monkeypatch):
    f = _fake(monkeypatch)
    f.locked = True
    out, why = desktops.move_window(7, "U2")
    assert out == "failure" and "could not locate" in why


def test_a_window_yabai_lost_is_told_apart_from_a_locked_screen(monkeypatch):
    f = _fake(monkeypatch)
    f.wins.append({"id": 8, "space": 1, "app": "Term", "title": "",
                   "frame": {"x": 0, "y": 0, "w": 500, "h": 400}})
    f.wins.insert(0, {"id": 6, "space": 1, "app": "Term", "title": "",
                      "frame": {"x": 0, "y": 0, "w": 500, "h": 400}})
    f.lost |= {6, 8}                                  # two lost at once: 7 is the one found
    assert desktops.lost(8) is True and desktops.lost(6) is True and desktops.lost(7) is False
    assert desktops.lost(99) is False                 # not listed at all
    out, why = desktops.move_window(8, "U2")
    assert out == "failure" and why.startswith(desktops.LOST) and "could not locate" in why
    f.locked = True                                   # nothing can be found: cannot be told
    assert desktops.lost(8) is None
    out, why = desktops.move_window(7, "U2")
    assert out == "failure" and not why.startswith(desktops.LOST)


def test_a_lone_window_yabai_cannot_find_is_unknown_not_lost(monkeypatch):
    f = _fake(monkeypatch)
    f.lost.add(7)
    assert desktops.lost(7) is None


def test_a_move_to_a_desktop_that_is_gone_says_so(monkeypatch):
    _fake(monkeypatch)
    assert desktops.move_window(7, "U9")[0] == "failure"


def test_a_window_that_cannot_be_found_after_a_move_is_unknown(monkeypatch):
    _fake(monkeypatch)
    assert desktops.move_window(99, "U2")[0] == "unknown"


def test_close_is_success_only_once_the_window_server_drops_the_id(monkeypatch):
    _fake(monkeypatch)
    assert desktops.close_window(7, wait=1, window_ids=lambda: {1, 2})[0] == "success"
    assert desktops.close_window(7, wait=0.6, window_ids=lambda: {7})[0] == "failure"


def test_the_fill_frame_is_the_usable_area_of_the_windows_screen():
    found = [((0, 0, 1728, 1117), (58, 0, 1670, 1084))]       # Dock on the left, menu bar on top
    win = {"frame": {"x": 10, "y": 30, "w": 587, "h": 467}}
    target = desktops.fill_frame(win, found)
    assert target == {"x": 58, "y": 33, "w": 1670, "h": 1084}
    assert desktops.fills(win, target) is False
    assert desktops.fills({"frame": dict(target)}, target) is True
    assert desktops.fills(win, None) is None and desktops.fill_frame({}, found) is None


def test_frame_window_reads_the_frame_back(monkeypatch):
    _fake(monkeypatch)
    target = {"x": 58, "y": 33, "w": 1670, "h": 1084}
    assert desktops.frame_window(7, target) == ("success", "")


def test_a_window_yabai_lost_is_sized_through_the_fallback(monkeypatch):
    f = _fake(monkeypatch)
    f.locked = True                                   # every window command fails
    target = {"x": 58, "y": 33, "w": 1670, "h": 1084}
    calls = []
    out = desktops.frame_window(7, target, fallback=lambda wid, t: calls.append(wid) or dict(t))
    assert out == ("success", "") and calls == [7]
    assert desktops.frame_window(7, target, fallback=lambda wid, t: None)[0] == "failure"


def test_unknown_listings_are_none_not_empty(monkeypatch):
    def broken(cmd, **k):
        class R:
            returncode, stdout, stderr = 0, "[", ""
        return R()
    monkeypatch.setattr(desktops.subprocess, "run", broken)
    assert desktops.all_windows() is None and desktops.space_of_window(7) is None


# ------------------------------------------------------------------------------- the browser

YW = [{"id": 1, "app": "Brave Browser", "title": "Inbox - Brave", "frame": {"x": 58, "y": 33}},
      {"id": 2, "app": "Brave Browser", "title": "Docs - Brave", "frame": {"x": 58, "y": 33}},
      {"id": 3, "app": "Brave Browser", "title": "", "frame": {"x": 100, "y": 100}},
      {"id": 4, "app": "Other", "title": "", "frame": {"x": 100, "y": 100}}]


def test_one_window_at_a_corner_is_matched_and_a_shared_corner_needs_the_name():
    assert brave.match_by_origin((100, 100), YW) == 3
    assert brave.match_by_origin((58, 33), YW) is None
    assert brave.match_by_origin((58, 33), YW, name="Docs") == 2


def test_the_join_eliminates_the_last_pair_at_a_corner():
    items = [{"origin": (58, 33), "name": "Inbox", "w": "a"}, {"origin": (58, 33), "name": "?", "w": "b"},
             {"origin": (100, 100), "name": "", "w": "c"}]
    joined = brave.join_by_origin(items, YW)
    assert {k: v["w"] for k, v in joined.items()} == {1: "a", 2: "b", 3: "c"}


def test_two_unnamed_windows_at_one_corner_stay_unjoined():
    items = [{"origin": (58, 33), "name": "", "w": "a"}, {"origin": (58, 33), "name": "", "w": "b"}]
    assert brave.join_by_origin(items, YW) == {}


def test_tabs_are_empty_when_the_browser_is_not_running_and_none_when_unknown(monkeypatch):
    monkeypatch.setattr(brave, "running", lambda app=brave.APP, env=None: False)
    assert brave.tabs() == []
    monkeypatch.setattr(brave, "running", lambda app=brave.APP, env=None: None)
    assert brave.tabs() is None


def test_a_closed_tab_is_success_only_when_gone_on_a_reread(monkeypatch):
    monkeypatch.setattr(brave, "osa", lambda *a, **k: (0, ""))
    monkeypatch.setattr(brave, "tabs", lambda app=brave.APP, env=None: [{"tab": "5"}])
    assert brave.close_tab("1", "5") == "failure"
    assert brave.close_tab("1", "6") == "success"
    assert brave.close_tab("1", "x") == "failure"
    monkeypatch.setattr(brave, "tabs", lambda app=brave.APP, env=None: None)
    assert brave.close_tab("1", "6") == "unknown"


def test_keep_front_gives_the_person_their_app_back(monkeypatch):
    given = []
    monkeypatch.setattr(screenshot, "activate", lambda b, env=None: given.append(b) or True)
    assert screenshot.keep_front("com.example.term", lambda: "com.brave.Browser", watch=0) == "com.example.term"
    assert given == ["com.example.term"]
    assert screenshot.keep_front("com.example.term", lambda: "com.example.term", watch=0) is None
    assert screenshot.keep_front(screenshot.LOGIN_WINDOW, lambda: "x", watch=0) is None
    assert screenshot.keep_front(None, lambda: "x", watch=0) is None


def test_keep_front_can_bring_the_app_back_through_the_callers_function(monkeypatch):
    monkeypatch.setattr(screenshot, "activate", lambda b, env=None: (_ for _ in ()).throw(AssertionError("default used")))
    mine = []
    assert screenshot.keep_front("com.example.term", lambda: "com.brave.Browser", watch=0,
                                 activate_app=mine.append) == "com.example.term"
    assert mine == ["com.example.term"]
