"""Do Not Disturb around driving a window: observed effects, and the person's Focus kept."""
import json

from claude_human import focus


def _store(tmp_path, modes):
    p = tmp_path / "Assertions.json"
    p.write_text(json.dumps({"data": [{"storeAssertionRecords": [
        {"assertionDetails": {"assertionDetailsModeIdentifier": m}} for m in modes]}]}))
    return p


def test_active_reads_the_daemons_store_and_missing_is_none_running(tmp_path):
    assert focus.active(_store(tmp_path, ["com.apple.donotdisturb.mode.default"])) == [
        "com.apple.donotdisturb.mode.default"]
    assert focus.active(tmp_path / "missing.json") == []


def test_a_focus_already_running_is_left_alone_and_restored_untouched(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(focus.subprocess, "run", lambda *a, **k: ran.append(a))
    store = _store(tmp_path, ["personal"])
    got = focus.begin(assertions=store)
    assert got["ok"] and not got["changed"] and got["before"] == ["personal"] and not ran
    back = focus.restore(["personal"], assertions=store)
    assert back["ok"] and not back["changed"] and not ran


def test_missing_shortcuts_are_a_visible_answer(tmp_path, monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "something else\n", ""
    monkeypatch.setattr(focus.subprocess, "run", lambda *a, **k: R())
    got = focus.begin("dnd on", "dnd off", assertions=_store(tmp_path, []))
    assert got["ok"] is False and "make 'dnd on' and 'dnd off'" in got["detail"]


def test_set_dnd_is_observed_not_asserted(tmp_path, monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(focus.subprocess, "run", lambda *a, **k: R())
    ok, detail = focus.set_dnd(True, assertions=_store(tmp_path, []))      # ran, store disagrees
    assert ok is False and "Focus is off" in detail
    ok, detail = focus.set_dnd(True, assertions=_store(tmp_path, ["dnd"]))
    assert ok is True


def test_restore_turns_dnd_off_only_when_begin_turned_it_on(tmp_path, monkeypatch):
    assert focus.restore(None)["detail"] == "nothing had begun"
    store = _store(tmp_path, [])
    assert focus.restore([], assertions=store)["detail"] == "Do Not Disturb is already off"
    calls = []

    def run(cmd, **k):
        class R:
            returncode, stderr = 0, ""
            stdout = "dnd on\ndnd off\n"
        if cmd[1] == "run":
            calls.append(cmd[2])
            store.write_text(json.dumps({"data": []}))
        return R()
    monkeypatch.setattr(focus.subprocess, "run", run)
    on = _store(tmp_path, ["dnd"])
    got = focus.restore([], "dnd on", "dnd off", assertions=on)
    assert calls == ["dnd off"] and got["ok"] and got["changed"]
