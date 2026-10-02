"""claude_human.notify against a fake ntfy server running in a thread."""
import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_human import cli, notify


class FakeNtfy:
    """Records every request. ``status`` and ``delay`` change how it answers."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.delay = 0.0
        self.held = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, body):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                fake.requests.append({"method": "POST", "path": self.path,
                                      "headers": dict(self.headers), "raw": raw})
                if fake.delay:
                    threading.Event().wait(fake.delay)
                if fake.status != 200:
                    return self._answer(fake.status, json.dumps({"code": fake.status * 100, "error": "nope"}))
                msg = json.loads(raw)
                msg.update(id=f"m{len(fake.held)}", time=1700000000, event="message")
                fake.held.append(msg)
                self._answer(200, json.dumps(msg))

            def do_GET(self):
                fake.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
                if "since=7d" in self.path:
                    return self._answer(400, json.dumps({"error": "invalid since parameter"}))
                lines = [json.dumps({"event": "open", "id": "o"})] + [json.dumps(m) for m in fake.held]
                self._answer(200, "\n".join(lines) + "\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, topic="alerts"):
        return f"http://127.0.0.1:{self.port}/{topic}"


@pytest.fixture
def fake(monkeypatch):
    for k in (notify.ENV_HOLD, notify.ENV_TOKEN, notify.ENV_USER, notify.ENV_PASSWORD, notify.ENV_URL):
        monkeypatch.delenv(k, raising=False)
    f = FakeNtfy()
    f.thread.start()
    yield f
    f.server.shutdown()
    f.server.server_close()


def test_publishes_json_to_the_server_root_with_utf8(fake):
    n = notify.NtfyNotifier(fake.url())
    ok, detail = n.notify("Ενημέρωση ✓", "body text 🙂", priority="high", link="myapp://task/1",
                          tags=["warning", "cat:system"])
    assert ok and detail == "sent id=m0"
    r = fake.requests[0]
    assert r["path"] == "/" and r["headers"]["Content-Type"] == "application/json"
    body = json.loads(r["raw"].decode("utf-8"))
    assert body == {"topic": "alerts", "message": "body text 🙂", "priority": 4, "title": "Ενημέρωση ✓",
                    "tags": ["warning", "cat:system"], "click": "myapp://task/1"}
    assert "Authorization" not in r["headers"]


def test_server_under_a_path_keeps_the_path(fake):
    ok, _ = notify.NtfyNotifier(f"http://127.0.0.1:{fake.port}/ntfy/alerts").notify("t", "b")
    assert ok and fake.requests[0]["path"] == "/ntfy/"


def test_bearer_token_and_basic_auth(fake):
    notify.NtfyNotifier(fake.url(), token="tk_secret").notify("t", "b")
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer tk_secret"
    notify.NtfyNotifier(fake.url(), user="ann", password="pw").notify("t", "b")
    assert fake.requests[1]["headers"]["Authorization"] == "Basic " + base64.b64encode(b"ann:pw").decode()


def test_the_token_never_appears_in_repr():
    assert "tk_secret" not in repr(notify.NtfyNotifier("https://x.example/t", token="tk_secret"))


def test_attach_actions_and_markdown_are_carried(fake):
    acts = [{"action": "view", "label": "Open", "url": "https://example.org/x"}]
    ok, _ = notify.NtfyNotifier(fake.url()).notify("t", "b", attach="https://example.org/a.png",
                                                   actions=acts, markdown=True)
    body = json.loads(fake.requests[0]["raw"])
    assert ok and body["attach"] == "https://example.org/a.png" and body["actions"] == acts
    assert body["markdown"] is True


def test_http_error_is_a_failure_with_the_reason_never_an_exception(fake):
    fake.status = 403
    ok, detail = notify.NtfyNotifier(fake.url()).notify("t", "b")
    assert not ok and detail == "HTTP 403: nope"


def test_unreachable_server_and_timeout_fail_cleanly(fake):
    ok, detail = notify.NtfyNotifier("http://127.0.0.1:9/alerts", timeout=2).notify("t", "b")
    assert not ok and detail
    fake.delay = 1.0
    ok, detail = notify.NtfyNotifier(fake.url(), timeout=0.2).notify("t", "b")
    assert not ok and ("timed out" in detail.lower() or "timeout" in detail.lower())


def test_bad_url_or_priority_is_refused_without_a_request(fake):
    assert notify.NtfyNotifier("ftp://x/alerts").notify("t", "b")[1].startswith("refused")
    assert notify.NtfyNotifier(fake.url("bad topic!")).notify("t", "b")[1].startswith("refused")
    ok, detail = notify.NtfyNotifier(fake.url()).notify("t", "b", priority="loud")
    assert not ok and "unknown priority" in detail
    assert fake.requests == []


@pytest.mark.parametrize("field", ["link", "attach"])
def test_a_loopback_link_the_phone_would_open_is_refused(fake, field):
    ok, detail = notify.NtfyNotifier(fake.url()).notify("t", "b", **{field: "http://localhost:8080/x"})
    assert not ok and "points at this machine" in detail and fake.requests == []
    ok, _ = notify.NtfyNotifier(fake.url(), allow_loopback_links=True).notify(
        "t", "b", **{field: "http://127.0.0.1/x"})
    assert ok


def test_a_loopback_action_url_is_refused(fake):
    acts = [{"action": "http", "label": "Retry", "url": "http://[::1]:8787/retry"}]
    ok, detail = notify.NtfyNotifier(fake.url()).notify("t", "b", actions=acts)
    assert not ok and "action Retry" in detail


def test_publishing_to_a_loopback_server_is_fine(fake):
    assert notify.NtfyNotifier(f"http://localhost:{fake.port}/alerts").notify("t", "b")[0]


def test_hold_sends_nothing(fake, monkeypatch):
    ok, detail = notify.NtfyNotifier(fake.url(), hold="test run").notify("t", "b")
    assert not ok and detail == "held: test run, nothing sent"
    monkeypatch.setenv(notify.ENV_HOLD, "1")
    ok, detail = notify.NtfyNotifier(fake.url()).notify("t", "b")
    assert not ok and notify.ENV_HOLD in detail
    assert fake.requests == []
    monkeypatch.setenv(notify.ENV_HOLD, "0")
    assert notify.NtfyNotifier(fake.url()).notify("t", "b")[0]


def test_poll_reads_back_messages_and_turns_days_into_hours(fake):
    n = notify.NtfyNotifier(fake.url())
    n.notify("one", "first")
    n.notify("two", "second")
    ok, msgs = n.poll("7d")
    assert ok and [m["message"] for m in msgs] == ["first", "second"]
    assert "since=168h" in fake.requests[-1]["path"] and "poll=1" in fake.requests[-1]["path"]


def test_go_since_and_priorities():
    assert notify.go_since("2d") == "48h" and notify.go_since(90) == "90s"
    assert notify.go_since("all") == "all" and notify.go_since("30m") == "30m"
    assert notify.priority_number(None) == 3 and notify.priority_number("urgent") == 5
    assert notify.priority_number("2") == 2
    with pytest.raises(ValueError):
        notify.priority_number(9)


def test_loopback_detection():
    for u in ("http://localhost/x", "https://127.0.0.2/x", "http://[::1]/", "http://0.0.0.0:1/",
              "http://app.localhost/"):
        assert notify.is_loopback_url(u), u
    for u in ("https://ntfy.example.org/x", "myapp://task/1", "http://10.0.0.5/x"):
        assert not notify.is_loopback_url(u), u


def test_file_notifier_is_the_floor(tmp_path):
    f = notify.FileNotifier(tmp_path / "sub" / "log.jsonl")
    ok, detail = f.notify("t", "b ✓", priority="high", tags=["x"])
    assert ok and "log.jsonl" in detail
    entry = json.loads((tmp_path / "sub" / "log.jsonl").read_text(encoding="utf-8"))
    assert entry["title"] == "t" and entry["body"] == "b ✓" and entry["tags"] == ["x"]
    assert isinstance(f, notify.Notifier) and isinstance(notify.NtfyNotifier("https://x/t"), notify.Notifier)


def test_first_that_works_falls_back_and_keeps_the_reason(tmp_path):
    chain = notify.FirstThatWorks(notify.NtfyNotifier("http://127.0.0.1:9/alerts", timeout=2),
                                  notify.FileNotifier(tmp_path / "log"))
    ok, detail = chain.notify("t", "b")
    assert ok and detail.startswith("NtfyNotifier: ") and "FileNotifier: written" in detail


def test_watch_sender_matches_run_source(fake):
    send = notify.watch_sender(notify.NtfyNotifier(fake.url()), title="repo watcher", priority="high")
    r = send("my-chat", "2 new issues", transport="iterm")
    assert r["result"] == "sent"
    assert json.loads(fake.requests[0]["raw"])["title"] == "repo watcher"
    fake.status = 500
    assert send("my-chat", "x")["result"] == "failed"


def test_cli_reads_the_token_from_the_environment(fake, monkeypatch, capsys):
    monkeypatch.setenv("MY_NTFY_TOKEN", "tk_env")
    rc = cli.main(["notify", "--url", fake.url(), "--title", "Hi", "--priority", "4", "--tag", "bell",
                   "--token-env", "MY_NTFY_TOKEN", "hello"])
    assert rc == 0 and capsys.readouterr().out.startswith("OK sent id=")
    r = fake.requests[0]
    assert r["headers"]["Authorization"] == "Bearer tk_env"
    assert json.loads(r["raw"]) == {"topic": "alerts", "message": "hello", "priority": 4, "title": "Hi",
                                    "tags": ["bell"]}


def test_cli_url_from_environment_stdin_and_file_floor(fake, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(notify.ENV_URL, fake.url())
    args = cli.build_parser().parse_args(["notify", "-"])
    assert cli.send_notification(args, stdin=io.StringIO("from stdin")) == 0
    assert json.loads(fake.requests[0]["raw"])["message"] == "from stdin"
    fake.status = 502
    rc = cli.main(["notify", "--file", str(tmp_path / "floor.jsonl"), "lost"])
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL HTTP 502" in out and "file: written" in out
    assert json.loads((tmp_path / "floor.jsonl").read_text())["body"] == "lost"


def test_cli_without_a_url_says_so(monkeypatch, capsys):
    monkeypatch.delenv(notify.ENV_URL, raising=False)
    assert cli.main(["notify", "x"]) == 2
    assert notify.ENV_URL in capsys.readouterr().err


def test_cli_has_no_token_or_password_option():
    help_text = cli.build_parser()._subparsers._group_actions[0].choices["notify"].format_help()
    assert "--token " not in help_text and "--password" not in help_text
