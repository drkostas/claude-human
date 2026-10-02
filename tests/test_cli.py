import io
from pathlib import Path

import pytest

from claude_human import cli, paths


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def test_no_command_takes_a_password_argument():
    p = cli.build_parser()
    text = p.format_help()
    for action in p._subparsers._group_actions[0].choices.values():
        text += action.format_help()
    assert "--password" not in text and "--pw" not in text


def test_build_tools_defaults_and_options(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_BIN_DIR, str(tmp_path))
    cmds = cli.build_commands(parse("build-tools"))
    assert [c[-1] for c in cmds] == [str(tmp_path / "sckshot.app"), str(tmp_path / "vhid_type")]
    assert all(c[0] == "bash" and c[1].endswith("build.sh") and Path(c[1]).is_file() for c in cmds)
    only = cli.build_commands(parse("build-tools", "--only", "vhid", "--out", str(tmp_path / "o")))
    assert [c[-1] for c in only] == [str(tmp_path / "o" / "vhid_type")]


def test_build_settings_reach_the_scripts_through_the_environment(monkeypatch):
    monkeypatch.delenv("CLAUDE_HUMAN_BUNDLE_ID", raising=False)
    env = cli.build_env(parse("build-tools", "--identity", "none", "--bundle-id", "org.example.shot",
                              "--usage-text", "why", "--pqrs-commit", "abc"))
    assert env["CLAUDE_HUMAN_IDENTITY"] == "none"
    assert env["CLAUDE_HUMAN_BUNDLE_ID"] == "org.example.shot"
    assert env["CLAUDE_HUMAN_USAGE"] == "why"
    assert env["CLAUDE_HUMAN_PQRS_COMMIT"] == "abc"
    assert "CLAUDE_HUMAN_BUNDLE_ID" not in cli.build_env(parse("build-tools"))


def test_screenshot_arguments():
    a = parse("screenshot", "--out", "x.png", "--app", "Safari", "--max-width", "800")
    assert (a.out, a.app, a.window, a.max_width) == ("x.png", "Safari", None, 800)
    with pytest.raises(SystemExit):
        parse("screenshot", "--out", "x.png", "--app", "A", "--window", "3")
    with pytest.raises(SystemExit):
        parse("screenshot")


def test_unlock_attempts_are_capped():
    assert parse("unlock").attempts == 2
    with pytest.raises(SystemExit):
        parse("unlock", "--attempts", "5")


def test_password_is_read_from_stdin_without_its_newline():
    assert cli.read_password(io.StringIO("pa ss\n")) == "pa ss"
    assert cli.read_password(io.StringIO("")) == ""


def test_unlock_command_passes_stdin_and_helper(monkeypatch):
    from claude_human import unlock
    seen = {}
    monkeypatch.setattr(unlock, "unlock", lambda pw, **kw: seen.update(pw=pw, **kw) or (True, "Unlocked."))
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cr3t\n"))
    assert cli.main(["--vhid", "/b/vhid", "unlock"]) == 0
    assert seen == {"pw": "s3cr3t", "helper": "/b/vhid", "attempts": 2}


def test_approve_failure_exits_1(monkeypatch, capsys):
    from claude_human import unlock
    monkeypatch.setattr(unlock, "approve", lambda pw, **kw: (False, unlock.NO_PROMPT))
    monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
    assert cli.main(["approve"]) == 1
    assert capsys.readouterr().out.startswith("FAIL No password prompt")


def test_skill_command_installs_the_packaged_skill(tmp_path, capsys):
    assert cli.SKILL_FILE.is_file()
    assert cli.main(["skill", "--dir", str(tmp_path)]) == 0
    target = tmp_path / "claude-human" / "SKILL.md"
    assert capsys.readouterr().out.strip() == str(target)
    text = target.read_text()
    assert text == cli.SKILL_FILE.read_text()
    assert text.startswith("---\nname: claude-human\ndescription: ")
    assert parse("skill").dir == "~/.claude/skills"


def test_the_two_copies_of_the_skill_are_identical():
    repo_copy = Path(__file__).resolve().parents[1] / "skill" / "SKILL.md"
    assert repo_copy.read_text() == cli.SKILL_FILE.read_text()
