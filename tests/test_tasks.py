"""claude_human.tasks: single flight, completion only by observation, the chain and its floor."""
import json
import sqlite3
import sys
import threading

import pytest

from claude_human import cli, tasks
from claude_human.tasks import Handoff, SqliteTaskStore, TaskEngine, filter_chain, head

TRUE = [sys.executable, "-c", "raise SystemExit(0)"]
FALSE = [sys.executable, "-c", "raise SystemExit(1)"]


class FakeNotifier:
    def __init__(self, answer=(True, "m1"), raises=False):
        self.sent = []
        self.answer = answer
        self.raises = raises

    def notify(self, title, body, **opts):
        self.sent.append((title, body, opts))
        if self.raises:
            raise RuntimeError("server on fire")
        return self.answer


class FixedVerifier:
    def __init__(self, done):
        self.done = done
        self.seen = []

    def verify(self, task):
        self.seen.append(task.id)
        return self.done


@pytest.fixture
def store(tmp_path):
    return SqliteTaskStore(tmp_path / "tasks.db")


# ---- single flight ----

def test_second_request_joins_the_first_and_tells_nobody(store):
    n = FakeNotifier()
    eng = TaskEngine(store, notifier=n, link="app://task/{id}")
    a, new_a = eng.request("approve", "app://x", "needs it", owner="bot", steps="step one", verify=TRUE)
    b, new_b = eng.request("approve", "app://x", "asked again", owner="bot")
    assert (new_a, new_b) == (True, False) and a == b
    assert len(n.sent) == 1
    title, body, opts = n.sent[0]
    assert "approve" in title and "needs it" in body and opts["link"] == f"app://task/{a}"
    assert len(store.pending()) == 1


def test_a_different_subject_is_a_different_task(store):
    eng = TaskEngine(store)
    a, _ = eng.request("approve", "app://x", "r")
    b, _ = eng.request("approve", "app://y", "r")
    c, _ = eng.request("sign-in", "app://x", "r")
    assert len({a, b, c}) == 3


def test_racing_requests_open_one_task(store):
    ids, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        ids.append(store.open_task("approve", "app://race", "r", None)[0])

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(ids)) == 1 and len(store.pending()) == 1


def test_a_closed_task_does_not_block_a_new_one(store):
    eng = TaskEngine(store, verifier=FixedVerifier(True))
    a, _ = eng.request("approve", "app://x", "r", verify=TRUE)
    assert eng.verify_pending()
    b, new = eng.request("approve", "app://x", "again", verify=TRUE)
    assert new and a != b


# ---- completion only by observation ----

def test_closes_only_when_the_verify_command_exits_0(store):
    eng = TaskEngine(store)
    tid, _ = eng.request("approve", "app://x", "r", verify=FALSE)
    assert eng.verify_pending() == []
    assert store.get(tid).is_open
    hist = store.history()
    assert not any(h["verb"] == "task.done" for h in hist)


def test_a_passing_verify_closes_with_evidence(store):
    eng = TaskEngine(store)
    tid, _ = eng.request("approve", "app://x", "r", verify=TRUE)
    closed = eng.verify_pending()
    assert [t.id for t in closed] == [tid]
    t = store.get(tid)
    assert t.state == "done" and t.outcome == "success"
    done = [h for h in store.history() if h["verb"] == "task.done"]
    assert done[0]["detail"] == {"verified_by": TRUE} and done[0]["origin"] == "observed"
    assert eng.verify_pending() == []          # nothing left, and nothing closes twice


def test_a_task_without_a_verify_command_never_closes(store):
    v = FixedVerifier(True)
    eng = TaskEngine(store, verifier=v)
    tid, _ = eng.request("plug-in", "device://cable", "r")
    assert eng.verify_pending() == [] and v.seen == []
    assert store.get(tid).is_open


def test_a_command_that_cannot_run_is_not_done(store, tmp_path):
    eng = TaskEngine(store, verifier=tasks.CommandVerifier(timeout=0.5))
    a, _ = eng.request("approve", "app://missing", "r", verify=[str(tmp_path / "no-such-binary")])
    b, _ = eng.request("approve", "app://slow", "r",
                       verify=[sys.executable, "-c", "import time; time.sleep(5)"])
    assert eng.verify_pending() == []
    assert store.get(a).is_open and store.get(b).is_open
    assert "timed out" in eng.verifier.last or "could not run" in eng.verifier.last


def test_a_shell_string_is_refused(store):
    with pytest.raises(TypeError):
        store.open_task("approve", "app://x", "r", None, verify="test -f /tmp/x")
    task = tasks.Task(id="t", capability="c", subject="s", verify="true")
    assert tasks.CommandVerifier().verify(task) is False


def test_done_button_asks_for_the_check(store):
    eng = TaskEngine(store, verifier=FixedVerifier(False))
    tid, _ = eng.request("approve", "app://x", "r", verify=TRUE)
    out = eng.done(tid)
    assert out["done"] is False and out["still_pending"] is True
    eng.verifier = FixedVerifier(True)
    assert eng.done(tid)["done"] is True
    assert eng.done(tid)["still_pending"] is False
    assert eng.done("nope")["detail"] == "no such task"


def test_complete_twice_closes_once(store):
    tid, _ = store.open_task("approve", "app://x", "r", None)
    assert store.complete(tid, "success", {"seen": 1}) is True
    assert store.complete(tid, "success", {"seen": 2}) is False


# ---- withdraw and comments ----

def test_withdraw_closes_without_success(store):
    tid, _ = store.open_task("approve", "app://x", "r", None)
    out = store.withdraw(tid, "owner", "not needed")
    assert out["ok"] and out["outcome"] == "success"
    t = store.get(tid)
    assert t.state == "withdrawn" and not t.is_open
    assert store.withdraw(tid, "owner", "again")["ok"] is False
    assert store.withdraw("nope", "owner", None)["ok"] is False
    assert TaskEngine(store, verifier=FixedVerifier(True)).verify_pending() == []


def test_a_comment_is_kept_and_does_not_close(store):
    tid, _ = store.open_task("approve", "app://x", "r", None)
    out = store.comment(tid, "the setting is not there", "owner")
    assert out["ok"] and out["intent"] == tid and out["subject"] == "app://x"
    assert store.get(tid).is_open
    assert store.comment(tid, "   ", "owner")["ok"] is False
    assert store.comment("nope", "hi", "owner")["ok"] is False
    assert [c["text"] for c in store.comments(tid)] == ["the setting is not there"]


def test_events_cannot_be_edited(store):
    store.open_task("approve", "app://x", "r", None)
    c = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM event")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE event SET outcome = 'x'")


# ---- authorizer and notifier ----

def test_a_refusal_is_recorded_and_raised(store):
    n = FakeNotifier()
    eng = TaskEngine(store, notifier=n, authorizer=tasks.Allow(actors={"owner"}))
    with pytest.raises(tasks.Refused):
        eng.request("approve", "app://x", "r", actor="stranger")
    assert store.pending() == [] and n.sent == []
    refused = [h for h in store.history() if h["verb"] == "task.refused"]
    assert refused and refused[0]["actor"] == "stranger" and refused[0]["subject"] == "app://x"
    tid, new = eng.request("approve", "app://x", "r", actor="owner")
    assert new


def test_a_notifier_that_raises_does_not_lose_the_task(store):
    eng = TaskEngine(store, notifier=FakeNotifier(raises=True))
    tid, new = eng.request("approve", "app://x", "r")
    assert new and store.get(tid).is_open
    notes = [h for h in store.history() if h["verb"] == "task.notify"]
    assert notes[0]["outcome"] == "failure"


def test_notify_hold_reaches_nobody(store, monkeypatch):
    from claude_human import notify
    monkeypatch.setenv("CLAUDE_HUMAN_NOTIFY_HOLD", "1")
    n = notify.NtfyNotifier("http://127.0.0.1:9/topic")
    eng = TaskEngine(store, notifier=n)
    eng.request("approve", "app://x", "r")
    notes = [h for h in store.history() if h["verb"] == "task.notify"]
    assert notes[0]["outcome"] == "failure" and "held" in notes[0]["detail"]["detail"]


# ---- the chain ----

def chain_of():
    return [Handoff("vnc", "https://station/view", "Open the window", 10, "any"),
            Handoff("url", "x-settings:privacy", "Open settings", 20, "darwin"),
            Handoff("url", "intent://settings", "Open settings", 30, "android"),
            Handoff("shell", None, "Nothing to open", 5, "any")]


def test_filter_keeps_what_the_touchpoint_can_show():
    got = filter_chain(chain_of(), ["steps", "url"], "android", steps="do this")
    assert [(h.kind, h.target) for h in got] == [("url", "intent://settings"), ("steps", "do this")]


def test_the_floor_is_added_last_and_only_once():
    got = filter_chain(chain_of(), ["steps", "vnc"], "ios", steps="do this")
    assert [h.kind for h in got] == ["vnc", "steps"] and got[-1].preference == tasks.FLOOR_PREFERENCE
    declared = [Handoff("steps", "my own words", None, 1)]
    got = filter_chain(declared, ["steps"], "any", steps="other words")
    assert [h.target for h in got] == ["my own words"]


def test_no_floor_without_steps_or_support():
    assert filter_chain(chain_of(), ["url"], "linux", steps="x") == []
    assert filter_chain([], ["steps"], "any", steps=None) == []


def test_head_skips_the_words():
    chain = [Handoff("steps", "w", None, 1), Handoff("url", "u", None, 2)]
    assert head(chain).kind == "url"
    assert head([Handoff("steps", "w")]) is None
    assert head([{"kind": "vnc", "target": "t"}]) == {"kind": "vnc", "target": "t"}


def test_card_carries_chain_and_head(store):
    eng = TaskEngine(store)
    tid, _ = eng.request("approve", "app://x", "r", steps="the steps", handoffs=chain_of())
    card = eng.card(store.get(tid), ["steps", "vnc", "url"], "android")
    assert [h["kind"] for h in card["handoffs"]] == ["vnc", "url", "steps"]
    assert card["head"]["kind"] == "vnc" and card["requires"] == "remote"
    for key in ("intent", "subject", "verb", "tier", "reason", "steps", "waiting_s"):
        assert key in card
    only_words = eng.card(store.get(tid), ["steps"], "android")
    assert only_words["head"] is None and only_words["handoffs"][0]["kind"] == "steps"


def test_open_prepares_the_head(store, tmp_path):
    flag = tmp_path / "prepared"
    eng = TaskEngine(store)
    tid, _ = eng.request("approve", "app://x", "r", steps="s", handoffs=[
        Handoff("url", "https://example.org", prepare=[sys.executable, "-c",
                                                       f"open({str(flag)!r}, 'w').close()"])])
    out = eng.open(tid, ["steps", "url"], "any")
    assert out["prepared"] is True and out["kind"] == "url" and flag.exists()
    words, _ = eng.request("plug-in", "device://cable", "r", steps="plug it in")
    assert eng.open(words, ["steps"], "any")["prepared"] is None


# ---- HTTP ----

@pytest.fixture
def served(store):
    from claude_human.tasks import server
    eng = TaskEngine(store, verifier=FixedVerifier(False))
    srv = server.make_server(eng, token="secret-token", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield eng, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(base, path, method="GET", body=None, token="secret-token"):
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_needs_the_token(served):
    _eng, base = served
    assert call(base, "/pending", token=None)[0] == 401
    assert call(base, "/pending", token="wrong")[0] == 401
    assert call(base, "/health") == (200, {"ok": True})


def test_http_routes_answer_the_app_shapes(served):
    eng, base = served
    tid, _ = eng.request("approve", "app://x", "needs it", steps="the steps",
                         verify=TRUE, handoffs=[Handoff("url", "https://example.org", "Open")])
    code, body = call(base, "/pending?supports=steps,url&platform=android")
    assert code == 200
    task = body["pending"][0]
    assert task["intent"] == tid and task["verb"] == "approve" and task["head"]["kind"] == "url"
    assert set(task["handoffs"][0]) >= {"kind", "target", "label", "preference", "platform"}
    code, body = call(base, f"/task/{tid}?supports=steps")
    assert code == 200 and body["task"]["head"] is None
    assert call(base, "/task/nope")[0] == 404

    code, body = call(base, f"/done/{tid}", "POST")
    assert body["done"] is False and body["still_pending"] is True
    eng.verifier = FixedVerifier(True)
    assert call(base, f"/done/{tid}", "POST")[1]["done"] is True

    t2, _ = eng.request("approve", "app://y", "r")
    code, body = call(base, f"/comment/{t2}", "POST", {"text": "wrong setting"})
    assert body["ok"] is True and body["subject"] == "app://y"
    assert call(base, f"/comment/{t2}", "POST", {"text": ""})[1]["ok"] is False
    code, body = call(base, f"/withdraw/{t2}", "POST", {"reason": "not needed"})
    assert body["ok"] is True and body["outcome"] == "success"
    assert call(base, "/pending")[1]["pending"] == []
    hist = call(base, "/history?limit=20")[1]["history"]
    assert {"task.open", "task.done", "task.comment", "task.withdraw"} <= {h["verb"] for h in hist}
    assert call(base, "/comments")[1]["comments"][0]["text"] == "wrong setting"


def test_http_answers_503_without_a_token(store, tmp_path):
    from claude_human.tasks import server
    srv = server.make_server(TaskEngine(store), token_file=tmp_path / "missing", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(f"http://127.0.0.1:{srv.server_address[1]}", "/pending")[0] == 503
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_refuses_a_network_address(store):
    from claude_human.tasks import server
    for host in ("0.0.0.0", "192.0.2.10", "example.org"):
        with pytest.raises(ValueError):
            server.make_server(TaskEngine(store), token="t", host=host, port=0)


# ---- CLI ----

def test_cli_open_list_verify(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_HUMAN_NOTIFY_HOLD", "1")
    monkeypatch.delenv("CLAUDE_HUMAN_NTFY_URL", raising=False)
    db = str(tmp_path / "t.db")
    flag = tmp_path / "flag"
    assert cli.main(["task", "--db", db, "open", "approve", "app://x", "--reason", "r",
                     "--steps", "s", "--handoff", "url=https://example.org", "--",
                     sys.executable, "-c", f"import os; raise SystemExit(0 if os.path.exists({str(flag)!r}) else 1)"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["new"] is True
    assert cli.main(["task", "--db", db, "open", "approve", "app://x", "--reason", "again"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": first["id"], "new": False}
    assert cli.main(["task", "--db", db, "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["head"]["target"] == "https://example.org"
    assert cli.main(["task", "--db", db, "verify", first["id"]]) == 1
    flag.write_text("")
    capsys.readouterr()
    assert cli.main(["task", "--db", db, "verify", first["id"]]) == 0
    assert "done" in capsys.readouterr().out


def test_cli_bad_handoff(tmp_path, capsys):
    assert cli.main(["task", "--db", str(tmp_path / "t.db"), "open", "a", "b", "--reason", "r",
                     "--handoff", "nokind"]) == 2


# ---- answers ----

def test_a_success_must_name_what_was_observed(store):
    tid, _ = store.open_task("approve", "app://x", "r", None)
    for bad in (None, "", "  ", {}):
        with pytest.raises(ValueError):
            store.complete(tid, "success", bad)
    with pytest.raises(ValueError):
        tasks.check_answer("maybe", "seen")
    assert store.get(tid).is_open


def test_unknown_is_recorded_and_leaves_the_task_open(store):
    tid, _ = store.open_task("approve", "app://x", "r", None)
    assert store.complete(tid, "unknown", {"note": "could not look"}) is False
    assert store.get(tid).is_open
    assert [h["verb"] for h in store.history()][0] == "task.unknown"
