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


def test_start_opens_a_browser_that_is_not_running_with_its_last_session():
    p = FakeProcs(pids=(), answers_after=2)
    outcome, detail = brave.start(procs=p)
    assert outcome == "success" and p.prepared == 1 and p.opened == 1 and p.asked == 2 and p.signals == []
    p = FakeProcs(pids=(), answers_after=10**6)
    assert brave.start(procs=p, answer_wait=9)[0] == "failure"
    p = FakeProcs(pids=(), opens=False)
    assert brave.start(procs=p)[0] == "failure"


def test_start_leaves_a_running_browser_alone():
    p = FakeProcs()
    outcome, detail = brave.start(procs=p)
    assert outcome == "skipped" and p.prepared == 0 and p.opened == 0 and p.signals == []


def test_restart_refuses_without_exactly_one_process():
    p = FakeProcs(pids=())
    assert brave.restart(procs=p)[0] == "failure" and p.signals == []
    p = FakeProcs(answers_after=10**6)
    assert brave.restart(procs=p, answer_wait=9)[0] == "failure"


def test_a_wait_for_on_the_result_is_refused_before_any_window_opens(monkeypatch, capsys):
    # wait_for is checked before the script runs and only the script sets the result, so waiting
    # for the result can never hold; it used to wait out the whole timeout
    opened = []
    monkeypatch.setattr(brave, "new_window", lambda *a, **k: opened.append(1) or ("1", None))
    try:
        brave.run("https://example.org/", "window.__claude_r = 'x'", wait_for="window.__claude_r")
    except ValueError as e:
        assert "before the script runs" in str(e)
    else:
        raise AssertionError("a wait on the result was not refused")
    assert opened == []
    assert brave.main(["https://example.org/", __file__, "--wait-for", "window.__claude_r !== undefined"]) == 2
    assert "can never hold" in capsys.readouterr().err and opened == []


def test_close_tab_reads_back_until_the_tab_is_gone(monkeypatch):
    listings = [[{"window": "1", "tab": "7", "url": "u", "title": "t"}], []]
    monkeypatch.setattr(brave, "osa", lambda *a, **k: (0, ""))
    monkeypatch.setattr(brave, "tabs", lambda *a, **k: listings.pop(0) if len(listings) > 1 else listings[0])
    slept = []
    assert brave.close_tab("1", "7", sleep=slept.append) == "success" and slept == [brave.CLOSE_GAP]


def test_close_tab_fails_only_when_every_read_still_lists_it(monkeypatch):
    still = [{"window": "1", "tab": "7", "url": "u", "title": "t"}]
    monkeypatch.setattr(brave, "osa", lambda *a, **k: (0, ""))
    monkeypatch.setattr(brave, "tabs", lambda *a, **k: still)
    slept = []
    assert brave.close_tab("1", "7", sleep=slept.append) == "failure" and len(slept) == brave.CLOSE_READS - 1
    monkeypatch.setattr(brave, "tabs", lambda *a, **k: None)
    assert brave.close_tab("1", "7", sleep=lambda s: None) == "unknown"
    assert brave.close_tab("1", "x7", sleep=lambda s: None) == "failure"


# Sending long scripts and files into a page, against a fake page that keeps what it is sent the way
# the browser does and runs the checks the page would run.
import base64
import re
import zlib


class FakePage:
    """Stands in for osascript and the page. `lose` drops that append (0-based) and `swap` holds that
    append back until the next one has landed, while the lengths it reports stay true."""

    def __init__(self, lose=None, swap=None, state="single", done=(), fail_append=None):
        self.store, self.calls, self.ran, self.files = {}, [], [], []
        self.lose, self.swap, self.state, self.done = lose, swap, state, list(done)
        self.fail_append, self.appends, self.held = fail_append, 0, None

    @staticmethod
    def js(script):
        body = script.split(' javascript "', 1)[1][:-1]
        return re.sub(r'\\(.)', r'\1', body)

    def __call__(self, script, timeout=30, *, env=None):
        self.calls.append(script)
        js = self.js(script)
        m = re.fullmatch(r'\(window\.__claude_in=window\.__claude_in\|\|\{\}\)\["(\w+)"\]="";"0"', js)
        if m:
            self.store[m[1]] = ""
            return 0, "0"
        m = re.fullmatch(r'window\.__claude_in\["(\w+)"\]\+=(.*);String\(window\.__claude_in\["\w+"\]\.length\)',
                         js, re.S)
        if m:
            n, self.appends = self.appends, self.appends + 1
            if n == self.fail_append:
                return 1, f"execution error: bad script near {m[2][:120]} (-2741)"
            part = json.loads(m[2])
            if n != self.lose:
                if n == self.swap:
                    self.held = part
                else:
                    self.store[m[1]] += part
                    if self.held is not None:
                        self.store[m[1]] += self.held
                        self.held = None
            units = len(self.store[m[1]].encode("utf-16-le", "surrogatepass")) // 2
            held = len(self.held.encode("utf-16-le", "surrogatepass")) // 2 if self.held else 0
            return 0, str(units + held)
        m = re.search(r'\)\(window\.__claude_in\["(\w+)"\]\)\)$', js)
        if m and js.startswith("String((function(s){"):
            return 0, str(zlib.crc32(self.store[m[1]].encode("utf-16-le", "surrogatepass")))
        m = re.fullmatch(r'window\.__claude_in&&delete window\.__claude_in\["(\w+)"\];"ok"', js)
        if m:
            self.store.pop(m[1], None)
            return 0, "ok"
        m = re.search(r'return \(0,eval\)\(s\);\}\)\("(\w+)"\)$', js)
        if m:
            self.ran.append(self.store.pop(m[1]))
            return 0, "ran"
        if "return e.multiple?" in js:
            return 0, self.state
        if "new DataTransfer()" in js:
            spec = json.loads(re.search(r'\}\)\(".*?",(\[.*\])\)$', js, re.S)[1])
            self.files = [(s["name"], s["type"], base64.b64decode(self.store[s["key"]])) for s in spec]
            return 0, str(len(spec))
        if js.startswith("(function(){var v=("):
            return 0, self.done.pop(0) if len(self.done) > 1 else self.done[0]
        self.ran.append(js)
        return 0, "direct"


def _text(n):
    # quotes, backslashes, newlines, accents and a character outside the BMP, so an escape or a
    # surrogate pair cut in half shows; numbered, so no two parts are alike and an order change shows
    out, size, i = [], 0, 0
    while size < n:
        out.append(f'{i}a"b\\c\nδ€😀 ')
        size, i = size + len(out[-1]), i + 1
    return "".join(out)[:n]


def test_a_short_script_still_goes_in_one_call(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(brave, "osa", page)
    assert brave.js_in("7", 'document.title + "x"') == "direct"
    assert page.ran == ['document.title + "x"'] and len(page.calls) == 1
    assert page.calls[0] == ('tell application "Brave Browser" to execute active tab of (first window '
                             'whose id is 7) javascript "document.title + \\"x\\""')


def test_a_long_script_reaches_the_page_whole_and_in_order(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(brave, "osa", page)
    code = _text(1_500_000)
    assert brave.js_in("7", code, chunk=200_000) == "ran"
    assert page.ran == [code] and page.store == {}
    assert len(page.calls) > 9 and max(len(c) for c in page.calls) < 200_000 + 300


def test_a_long_applescript_goes_to_osascript_on_standard_input(monkeypatch):
    # an argument list is held to about 1 MB, standard input is not
    seen = []

    def fake_run(cmd, **k):
        seen.append((cmd, k.get("input")))

        class R:
            returncode, stdout, stderr = 0, "ok\n", ""
        return R()
    monkeypatch.setattr(brave.subprocess, "run", fake_run)
    long = 'return "' + "A" * 2_000_000 + '"'
    assert brave.osa("return 1") == (0, "ok") and brave.osa(long) == (0, "ok")
    assert seen == [(["/usr/bin/osascript", "-e", "return 1"], None), (["/usr/bin/osascript", "-"], long)]


def test_parts_cut_small_keep_every_character(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(brave, "osa", page)
    code = _text(5_000)
    assert brave.js_in("7", code, chunk=64) == "ran" and page.ran == [code]
    parts = brave._parts(code, 64)
    assert "".join(parts) == code and all(len(brave._escape(json.dumps(p))) <= 64 for p in parts)


def test_a_lost_part_raises_and_nothing_runs(monkeypatch):
    page = FakePage(lose=2)
    monkeypatch.setattr(brave, "osa", page)
    try:
        brave.js_in("7", _text(5_000), chunk=500)
    except RuntimeError as e:
        assert "part 3 of" in str(e) and "did not arrive whole" in str(e)
    else:
        raise AssertionError("a lost part was not noticed")
    assert page.ran == [] and page.store == {}


def test_parts_out_of_order_raise_and_nothing_runs(monkeypatch):
    page = FakePage(swap=1)
    monkeypatch.setattr(brave, "osa", page)
    try:
        brave.js_in("7", _text(5_000), chunk=500)
    except RuntimeError as e:
        assert "differ from what was sent" in str(e)
    else:
        raise AssertionError("parts out of order were not noticed")
    assert page.ran == [] and page.store == {}


def test_upload_sets_the_files_and_waits_until_done_holds_still(monkeypatch, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.bin"
    a.write_bytes(b"%PDF-1.4 " + bytes(range(256)) * 3000)
    b.write_bytes(b"\x00\x01\x02")
    page = FakePage(state="multiple", done=["", "a", "a.pdf", "a.pdf", "a.pdf"])
    monkeypatch.setattr(brave, "osa", page)
    monkeypatch.setattr(brave.time, "sleep", lambda s: None)
    got = brave.upload("7", "#receipt", [a, b], done="document.querySelector('#name').value", settle=0,
                       chunk=10_000)
    assert got == {"files": ["a.pdf", "b.bin"], "bytes": [a.stat().st_size, 3], "done": "a.pdf"}
    assert page.files == [("a.pdf", "application/pdf", a.read_bytes()),
                          ("b.bin", "application/octet-stream", b"\x00\x01\x02")]
    assert page.store == {}


def test_upload_refuses_before_sending_anything(monkeypatch, tmp_path):
    f = tmp_path / "r.pdf"
    f.write_bytes(b"x" * 100)
    for page, kwargs, why in [
            (FakePage(), {"paths": f, "max_bytes": 99}, "over the 99-byte limit"),
            (FakePage(), {"paths": [f, f]}, "takes one file"),
            (FakePage(state="missing"), {"paths": f}, "no element matches"),
            (FakePage(state="not-file"), {"paths": f}, "is not an <input type=file>"),
            (FakePage(), {"paths": tmp_path / "gone.pdf"}, "is not a file")]:
        monkeypatch.setattr(brave, "osa", page)
        try:
            brave.upload("7", "#receipt", **kwargs)
        except ValueError as e:
            assert why in str(e), (why, str(e))
        else:
            raise AssertionError(f"not refused: {why}")
        assert page.appends == 0 and page.files == []


def test_an_upload_error_carries_no_file_contents(monkeypatch, tmp_path):
    f = tmp_path / "r.pdf"
    f.write_bytes(b"secret-receipt-number-0123456789" * 50)
    page = FakePage(fail_append=0)
    monkeypatch.setattr(brave, "osa", page)
    try:
        brave.upload("7", "#receipt", f)
    except RuntimeError as e:
        encoded = base64.b64encode(f.read_bytes()).decode()
        assert "did not reach the page" in str(e) and encoded[:24] not in str(e)
    else:
        raise AssertionError("a failed part was not reported")
    assert page.store == {}


def test_settled_needs_the_value_to_hold_for_the_whole_settle_time():
    now = [0.0]
    seq = iter(["", "a", "ab", "ab", "ab", "ab", "ab"])
    got = brave._settled(lambda: next(seq), settle=1.0, timeout=60, step=0.5, clock=lambda: now[0],
                         sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert got == "ab" and now[0] == 2.0   # "ab" first read at 1.0
    try:
        brave._settled(lambda: "", settle=1, timeout=3, clock=lambda: now[0],
                       sleep=lambda s: now.__setitem__(0, now[0] + s))
    except TimeoutError as e:
        assert "did not settle" in str(e)
    else:
        raise AssertionError("an empty value settled")


def test_upload_command(monkeypatch, tmp_path, capsys):
    f = tmp_path / "r.pdf"
    f.write_bytes(b"pdf")
    page = FakePage()
    monkeypatch.setattr(brave, "osa", page)
    assert brave.main(["upload", "7", "#receipt", str(f)]) == 0
    assert json.loads(capsys.readouterr().out) == {"files": ["r.pdf"], "bytes": [3], "done": None}
    assert brave.main(["upload", "7", "#receipt", str(f), "--max-bytes", "2"]) == 2
    assert brave.main(["upload", "7"]) == 2


def test_a_consent_prompt_on_screen_fails_the_run_at_once(monkeypatch, capsys, tmp_path):
    # behind a consent prompt each AppleScript call waits out its own timeout, and a run with
    # --timeout 90 went on for more than six minutes; now it says why before it opens anything
    opened = []
    monkeypatch.setattr(brave, "new_window", lambda *a, **k: opened.append(1) or ("1", None))
    try:
        brave.run("https://example.org/", "1", blocker=lambda: "UserNotificationCenter")
    except brave.Blocked as e:
        assert str(e).startswith("blocked_by UserNotificationCenter")
    else:
        raise AssertionError("a run behind a consent prompt was not refused")
    assert opened == []
    monkeypatch.setattr(brave, "blocked_by", lambda: "UserNotificationCenter")
    js = tmp_path / "x.js"
    js.write_text("1")
    assert brave.main(["https://example.org/", str(js)]) == 1
    assert "blocked_by UserNotificationCenter" in capsys.readouterr().err and opened == []


def test_a_prompt_that_appears_during_the_wait_ends_it(monkeypatch):
    up = []
    monkeypatch.setattr(brave, "new_window", lambda *a, **k: ("1", None))
    monkeypatch.setattr(brave, "osa", lambda *a, **k: (0, ""))

    def js_in(*a, **k):
        up.append(1)           # the prompt is up from now on, and the browser does not answer
        raise RuntimeError("AppleEvent timed out")
    monkeypatch.setattr(brave, "js_in", js_in)
    try:
        brave.run("https://example.org/", "1", timeout=600, blocker=lambda: "UserNotificationCenter" if up else None)
    except brave.Blocked:
        pass
    else:
        raise AssertionError("the wait went on behind the prompt")
    assert len(up) == 1


def test_no_prompt_is_no_block(monkeypatch):
    import claude_human.consent as consent
    monkeypatch.setattr(consent, "prompts", lambda: [{"owner": "CoreServicesUIAgent"}])
    assert brave.blocked_by() is None
    monkeypatch.setattr(consent, "prompts", lambda: None)
    assert brave.blocked_by() is None
    monkeypatch.setattr(consent, "prompts", lambda: [{"owner": "UserNotificationCenter"}])
    assert brave.blocked_by() == "UserNotificationCenter"


def test_a_new_window_waits_by_the_clock_not_by_a_count_of_slow_listings():
    import time
    calls = []

    def slow_ids():
        calls.append(1)
        time.sleep(0.05)      # a listing that answers slowly, as AppleScript does behind a prompt
        return {"100"}
    t = time.monotonic()
    assert brave.new_window(ids=slow_ids, yids=lambda: None, make=lambda: None, wait=0.2, step=0.01) == (None, None)
    assert time.monotonic() - t < 1.0 and len(calls) < 20
