"""Working in the person's own browser: tab listing, window reuse, and yabai ids at birth."""
import json

from claude_human import brave


def test_tabs_are_parsed_from_the_bulk_listing():
    raw = "11\t21\thttps://example.org/a\tA\n11\t22\thttps://example.org/b\tB\n\n"
    assert brave.parse_tabs(raw) == [
        {"window": "11", "tab": "21", "url": "https://example.org/a", "title": "A"},
        {"window": "11", "tab": "22", "url": "https://example.org/b", "title": "B"}]
    assert brave.parse_tabs(None) is None


def test_the_listing_asks_by_property_not_tab_by_tab():
    script = brave._list_script("Brave Browser")
    assert "set urls to URL of tabs of w" in script and "repeat with t in" not in script
    assert "{window id 7}" in brave._list_script("Brave Browser", "7")


def test_a_new_window_and_its_yabai_id_are_the_ones_listed_after():
    ids, yids = {"100"}, {7000}

    def make():
        ids.add("200")
        yids.add(7041)

    assert brave.new_window(ids=lambda: set(ids), yids=lambda: set(yids), make=make, step=0.01) == ("200", 7041)


def test_two_new_yabai_windows_at_once_give_no_yabai_id():
    ids, yids = {"100"}, {7000}

    def make():
        ids.add("300")
        yids.update({7100, 7101})

    assert brave.new_window(ids=lambda: set(ids), yids=lambda: set(yids), make=make, step=0.01) == ("300", None)


def test_no_yabai_gives_no_yabai_id():
    ids = {"100"}
    assert brave.new_window(ids=lambda: set(ids), yids=lambda: None, make=lambda: ids.add("400"),
                            step=0.01) == ("400", None)


def test_a_kept_window_is_found_while_the_browser_has_it(tmp_path):
    brave.keep("claude-a", "200", 7041, state=tmp_path)
    assert json.loads((tmp_path / "claude-a.json").read_text()) == {"window": "200", "yabai": 7041}
    assert brave.kept("claude-a", state=tmp_path, open_ids={"200"}) == {"window": "200", "yabai": 7041}
    assert brave.kept("claude-a", state=tmp_path, open_ids={"100"}) is None
    assert brave.kept("claude-b", state=tmp_path, open_ids={"200"}) is None


def test_outside_tmux_there_is_no_default_key():
    assert brave.default_key(env={}) is None


def test_new_window_runs_every_command_with_the_callers_environment(monkeypatch):
    seen = []

    def fake_run(cmd, **k):
        seen.append(k.get("env"))

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(brave.subprocess, "run", fake_run)
    brave.new_window(env={"PATH": "/usr/bin"}, wait=0.2, step=0.1)
    assert seen and all(e == {"PATH": "/usr/bin"} for e in seen)


def _store(folder, *origins):
    import sqlite3
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "Login Data") as db:
        db.execute("CREATE TABLE logins (origin_url TEXT, password_value BLOB)")
        db.executemany("INSERT INTO logins VALUES (?, ?)", [(o, b"\x00enc") for o in origins])


def test_has_login_reads_every_profile(tmp_path):
    from claude_human import brave
    _store(tmp_path / "Default", "https://mail.example.org/")
    _store(tmp_path / "Profile 1", "https://shop.example.com/login")
    assert brave.has_login("shop.example.com", profiles=tmp_path) is True
    assert brave.has_login("example.org", profiles=tmp_path) is True
    assert brave.has_login("nowhere.example.net", profiles=tmp_path) is False


def test_has_login_is_unknown_when_no_store_reads(tmp_path):
    from claude_human import brave
    assert brave.has_login("example.org", profiles=tmp_path) is None
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Login Data").write_text("not a database")
    assert brave.has_login("example.org", profiles=tmp_path) is None


def test_has_login_command(tmp_path, monkeypatch):
    from claude_human import brave
    _store(tmp_path / "Default", "https://mail.example.org/")
    monkeypatch.setattr(brave, "PROFILES", tmp_path)
    monkeypatch.setattr(brave.has_login, "__kwdefaults__", {"profiles": tmp_path})
    assert brave.main(["has-login", "example.org"]) == 0
    assert brave.main(["has-login", "example.net"]) == 1
    assert brave.main(["has-login"]) == 2


def test_continue_where_left_off_marks_each_profile(tmp_path):
    import json as _j
    for prof in ("Default", "Profile 1", "System Profile"):
        (tmp_path / prof).mkdir()
        (tmp_path / prof / "Preferences").write_text(_j.dumps({"profile": {"exit_type": "Crashed", "name": prof}, "other": 1}))
    changed = brave.continue_where_left_off(tmp_path)
    d = _j.loads((tmp_path / "Default" / "Preferences").read_text())
    assert changed == ["Default", "Profile 1"]
    assert d["session"]["restore_on_startup"] == 1 and d["profile"]["exit_type"] == "Normal"
    assert d["profile"]["exited_cleanly"] is True and d["other"] == 1 and d["profile"]["name"] == "Default"
    assert _j.loads((tmp_path / "System Profile" / "Preferences").read_text())["profile"]["exit_type"] == "Crashed"


class FakeProcs:
    def __init__(self, pids=(200,), quits=True, answers_after=1, opens=True):
        self._pids, self.quits, self.answers_after, self.opens = list(pids), quits, answers_after, opens
        self.signals, self.prepared, self.opened, self.asked, self.dead = [], 0, 0, 0, False

    def pids(self):
        return self._pids

    def signal(self, pid, sig):
        import signal as _s
        self.signals.append(sig)
        if sig == _s.SIGKILL or self.quits:
            self.dead = True

    def alive(self, pid):
        return not self.dead

    def open(self):
        self.opened += 1
        return self.opens

    def answers(self):
        self.asked += 1
        return self.asked >= self.answers_after

    def prepare(self):
        self.prepared += 1
        return ["Default"]

    def sleep(self, s):
        pass


def test_restart_quits_then_reopens_the_last_session():
    import signal as _s
    p = FakeProcs(answers_after=2)
    outcome, detail = brave.restart(procs=p)
    assert outcome == "success" and detail["how"] == "quit" and p.signals == [_s.SIGTERM]
    assert p.prepared == 1 and p.opened == 1


def test_restart_stops_a_browser_that_will_not_quit():
    import signal as _s
    p = FakeProcs(quits=False)
    outcome, detail = brave.restart(procs=p, quit_wait=5)
    assert outcome == "success" and detail["how"] == "stopped" and p.signals == [_s.SIGTERM, _s.SIGKILL]


def test_restart_refuses_without_exactly_one_process():
    p = FakeProcs(pids=())
    assert brave.restart(procs=p)[0] == "failure" and p.signals == []
    p = FakeProcs(answers_after=10**6)
    assert brave.restart(procs=p, answer_wait=9)[0] == "failure"
