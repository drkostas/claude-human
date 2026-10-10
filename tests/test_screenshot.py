from claude_human import screenshot as s


def win(wid, owner, w, h, layer=0, title="", alpha=1.0, x=0, y=0):
    return {"kCGWindowNumber": wid, "kCGWindowOwnerName": owner, "kCGWindowName": title,
            "kCGWindowLayer": layer, "kCGWindowAlpha": alpha,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


def test_to_windows_keeps_ordinary_windows_only_and_sorts():
    raw = [win(1, "Safari", 1200, 800, title="b"), win(2, "Dock", 1500, 80),
           win(3, "Menu", 300, 300, layer=25), win(4, "Notes", 100, 100),
           win(5, "safari", 900, 700, title="a")]
    out = s.to_windows(raw)
    assert [w["id"] for w in out] == [5, 1]
    assert out[0] == {"id": 5, "app": "safari", "title": "a", "x": 0, "y": 0, "width": 900, "height": 700}


def test_pick_window_prefers_exact_name_then_largest():
    wins = s.to_windows([win(1, "Code Helper", 2000, 1500), win(2, "Code", 800, 600),
                         win(3, "Code", 1000, 900)])
    assert s.pick_window(wins, "code") == 3
    assert s.pick_window(wins, "helper") == 1
    assert s.pick_window(wins, "nothing") is None


def test_find_blocker_looks_at_every_layer_and_every_window():
    raw = [win(1, "Safari", 1200, 800), win(2, "Finder", 900, 700),
           win(3, "SecurityAgent", 400, 300, layer=1000)]
    assert s.find_blocker(raw) == "SecurityAgent"
    assert s.find_blocker(raw[:2]) is None


def test_auth_panel_is_a_sized_visible_securityagent_window():
    display = (1512, 982)
    helper = win(7, "SecurityAgent", 40, 40, layer=1000)
    lock_surface = win(8, "SecurityAgent", 1512, 982, layer=1000)
    hidden = win(9, "SecurityAgent", 400, 300, alpha=0)
    panel = win(10, "SecurityAgent", 420, 330, layer=1000, title="Login Items")
    other = win(11, "Safari", 420, 330)
    assert s.find_auth_panel([helper, lock_surface, hidden, other], display) is None
    assert s.find_auth_panel([helper, other, panel], display) == {
        "wid": 10, "owner": "SecurityAgent", "title": "Login Items"}


def test_sckshot_arguments_and_output():
    assert s.sckshot_args("/b/sckshot", "/t/x.jpg", None, 1100) == [
        "/b/sckshot", "--out", "/t/x.jpg", "--max-width", "1100"]
    assert s.sckshot_args("/b/sckshot", "/t/x.jpg", 42, 0)[-2:] == ["--window", "42"]
    assert s.parse_sckshot_output(b"OK 1100x700\n") == (1100, 700)
    assert s.parse_sckshot_output("OK 3x4") == (3, 4)
    assert s.parse_sckshot_output(b"capture: denied") is None


def test_frame_refuses_while_locked_without_a_panel(monkeypatch):
    captured = []
    monkeypatch.setattr(s, "capture", lambda wid=None, **kw: captured.append(wid) or (b"jpg", 1, 1))
    monkeypatch.setattr(s, "screen_locked", lambda: True)
    monkeypatch.setattr(s, "auth_prompt", lambda: None)
    assert s.frame(5) is None and captured == []
    # a credential panel sets the locked bit while the desktop stays live
    monkeypatch.setattr(s, "auth_prompt", lambda: {"wid": 1, "owner": "SecurityAgent", "title": ""})
    assert s.frame(5) == (b"jpg", 1, 1) and captured == [5]
    monkeypatch.setattr(s, "screen_locked", lambda: False)
    monkeypatch.setattr(s, "auth_prompt", lambda: None)
    assert s.frame(None) == (b"jpg", 1, 1)


def test_save_reports_a_missing_helper(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        s.save(tmp_path / "x.png", sckshot=tmp_path / "missing")


def test_resolve_with_an_id_is_that_window_only_while_it_is_the_apps(monkeypatch):
    wins = [{"id": 5, "app": "Safari", "title": "", "x": 0, "y": 0, "width": 100, "height": 100},
            {"id": 6, "app": "Safari", "title": "", "x": 0, "y": 0, "width": 900, "height": 900},
            {"id": 7, "app": "Notes", "title": "", "x": 0, "y": 0, "width": 900, "height": 900}]
    monkeypatch.setattr(s, "windows", lambda: list(wins))
    assert s.resolve("Safari", None) == 6          # the app alone: its largest window, as before
    assert s.resolve("Safari", 5) == 5             # both: that window, not the largest
    assert s.resolve("Safari", 7) is None          # another app's window is not this app's
    assert s.resolve("Safari", 99) is None         # a window that is gone is gone, never another
    assert s.resolve(None, 7) == 7


def test_front_window_is_the_first_ordinary_window_front_to_back():
    raw = [{"kCGWindowNumber": 1, "kCGWindowLayer": 25, "kCGWindowOwnerName": "Dock",
            "kCGWindowBounds": {"Width": 500, "Height": 500}},
           {"kCGWindowNumber": 2, "kCGWindowLayer": 0, "kCGWindowOwnerName": "Notes",
            "kCGWindowBounds": {"Width": 10, "Height": 10}},
           {"kCGWindowNumber": 3, "kCGWindowLayer": 0, "kCGWindowOwnerName": "Safari",
            "kCGWindowBounds": {"Width": 800, "Height": 600}},
           {"kCGWindowNumber": 4, "kCGWindowLayer": 0, "kCGWindowOwnerName": "Notes",
            "kCGWindowBounds": {"Width": 800, "Height": 600}}]
    assert s.front_window(raw) == 3
    assert s.front_window([]) is None
