"""Desktops through yabai: verified effects, labels over indices, and loud unreadable answers."""
import json

import pytest

from claude_human import desktops


class FakeYabai:
    """Answers yabai -m calls from a small model of the machine."""

    def __init__(self, spaces=None, create_works=True):
        self.spaces = spaces if spaces is not None else [
            {"uuid": "U1", "index": 1, "label": "one", "is-visible": True},
            {"uuid": "U2", "index": 2, "label": "", "is-visible": False}]
        self.create_works = create_works
        self.commands = []

    def run(self, cmd, **k):
        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        args = cmd[2:]
        self.commands.append(args)
        if args[:2] == ["query", "--spaces"]:
            r.stdout = json.dumps(self.spaces)
        elif args[:1] == ["space"]:
            rest = args[1:]
            if rest == ["--create"]:
                if self.create_works:
                    self.spaces.append({"uuid": f"U{len(self.spaces) + 1}", "index": len(self.spaces) + 1,
                                        "label": "", "is-visible": False})
            elif len(rest) == 3 and rest[1] == "--label":
                for s in self.spaces:
                    if str(s["index"]) == rest[0]:
                        s["label"] = rest[2]
            elif len(rest) == 2 and rest[1] == "--destroy":
                self.spaces = [s for s in self.spaces if s["label"] != rest[0]]
            elif rest[0] == "--focus":
                for s in self.spaces:
                    s["is-visible"] = str(s["index"]) == rest[1]
        return r


@pytest.fixture
def fake(monkeypatch):
    f = FakeYabai()
    monkeypatch.setattr(desktops.subprocess, "run", f.run)
    return f


def test_spaces_and_index_resolve_by_label(fake):
    assert desktops.spaces()[0] == {"uuid": "U1", "index": 1, "label": "one", "visible": True}
    assert desktops.index_of("one") == 1 and desktops.index_of("gone") is None
    assert desktops.index_of("") is None
    assert desktops.visible_label() == "one"


def test_create_finds_the_new_desktop_by_uuid_and_labels_it(fake):
    made = desktops.create("work")
    assert made == {"index": 3, "uuid": "U3"}
    assert desktops.index_of("work") == 3


def test_create_refuses_a_duplicate_label_and_a_noop_create(fake):
    with pytest.raises(RuntimeError, match="already exists"):
        desktops.create("one")
    fake.create_works = False
    with pytest.raises(RuntimeError, match="nothing was created"):
        desktops.create("fresh")


def test_a_forgotten_label_is_put_back_before_destroy(fake):
    assert desktops.relabel("U2", "mine") is True
    assert desktops.destroy("mine", "U2") is True
    assert desktops.index_of("mine") is None
    assert desktops.destroy("mine", "U2") is True          # gone twice is still True
    assert desktops.relabel("U-gone", "x") is False


def test_label_for_makes_a_stable_label_from_the_uuid(fake):
    fake.spaces[1]["uuid"] = "3B2A-99"
    assert desktops.label_for("3B2A-99") == "d-3b2a"
    assert desktops.label_for("U1") == "one"
    assert desktops.label_for("missing") is None


def test_focus_is_verified(fake):
    assert desktops.focus(2) is True
    assert desktops.visible_label() == ""                  # U2 has no label


def test_an_unreadable_answer_raises_instead_of_reading_empty(monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "[", ""
    monkeypatch.setattr(desktops.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="unreadable"):
        desktops.query("--spaces")
