import os
import shutil
import subprocess
import sys

import pytest

from claude_human import paths

SCRIPTS = [paths.tool_source("sckshot") / "build.sh", paths.tool_source("vhid") / "build.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.name)
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.name)
def test_scripts_need_an_output_path(script):
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert r.returncode != 0 and "usage" in r.stderr


def test_sckshot_build_rejects_a_bad_bundle_id_or_output_before_compiling(tmp_path):
    script = str(SCRIPTS[0])
    env = dict(os.environ, CLAUDE_HUMAN_BUNDLE_ID="bad id;rm")
    r = subprocess.run(["bash", script, str(tmp_path / "x.app")], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "invalid bundle id" in r.stderr
    r = subprocess.run(["bash", script, str(tmp_path / "x")], capture_output=True, text=True)
    assert r.returncode == 2 and ".app" in r.stderr


def test_build_scripts_carry_no_fixed_identity():
    text = SCRIPTS[0].read_text()
    assert "local.claude-human.sckshot" in text
    assert "Apple Development: " in text  # found at build time, never written in


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None,
                    reason="needs swiftc and ScreenCaptureKit (macOS)")
def test_sckshot_builds_unsigned(tmp_path):
    env = dict(os.environ, CLAUDE_HUMAN_IDENTITY="none", CLAUDE_HUMAN_BUNDLE_ID="org.example.sckshot")
    out = tmp_path / "sckshot.app"
    r = subprocess.run(["bash", str(SCRIPTS[0]), str(out)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (out / "Contents" / "MacOS" / "sckshot").is_file()
    assert "org.example.sckshot" in (out / "Contents" / "Info.plist").read_text()


PYAPP = paths.tool_source("pyapp") / "build.sh"


def test_python_app_script_parses_and_checks_its_inputs(tmp_path):
    assert subprocess.run(["bash", "-n", str(PYAPP)]).returncode == 0
    r = subprocess.run(["bash", str(PYAPP)], capture_output=True, text=True)
    assert r.returncode != 0 and "usage" in r.stderr
    env = dict(os.environ)
    env.pop("CLAUDE_HUMAN_BUNDLE_ID", None)
    r = subprocess.run(["bash", str(PYAPP), str(tmp_path / "x.app")], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "bundle id" in r.stderr
    r = subprocess.run(["bash", str(PYAPP), str(tmp_path / "x")], env=dict(env, CLAUDE_HUMAN_BUNDLE_ID="a.b"),
                       capture_output=True, text=True)
    assert r.returncode == 2 and ".app" in r.stderr


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/opt/homebrew/bin/python3"),
                    reason="needs macOS and Homebrew's Python")
def test_python_app_builds_once_and_runs_python(tmp_path):
    env = dict(os.environ, CLAUDE_HUMAN_BUNDLE_ID="org.example.pyapp", CLAUDE_HUMAN_IDENTITY="-",
               CLAUDE_HUMAN_APP_NAME="Example Agent")
    out = tmp_path / "Example Agent.app"
    r = subprocess.run(["bash", str(PYAPP), str(out)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    exe = r.stdout.strip()
    assert exe == str(out / "Contents" / "MacOS" / "example-agent")
    assert subprocess.run([exe, "-c", "import sys; print(sys.version_info[0])"],
                          capture_output=True, text=True).stdout.strip() == "3"
    assert "org.example.pyapp" in (out / "Contents" / "Info.plist").read_text()
    stamp = (out / "Contents" / "MacOS" / "example-agent").stat().st_mtime_ns
    r = subprocess.run(["bash", str(PYAPP), str(out)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == exe
    assert (out / "Contents" / "MacOS" / "example-agent").stat().st_mtime_ns == stamp  # not rebuilt


def _macho_uuid(path):
    out = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True).stdout
    return next(line.split()[1] for line in out.splitlines() if line.split()[:1] == ["uuid"])


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/opt/homebrew/bin/python3"),
                    reason="needs macOS and Homebrew's Python")
def test_python_app_never_shares_the_launchers_uuid(tmp_path):
    """macOS applies Local Network rules by a binary's UUID, so a copy that kept Python.app's UUID let
    a new bundle, denied until someone answers for it, cut Python off the LAN."""
    home = subprocess.run(["/opt/homebrew/bin/python3", "-c", "import sys;print(sys.base_prefix)"],
                          capture_output=True, text=True).stdout.strip()
    launcher = _macho_uuid(f"{home}/Resources/Python.app/Contents/MacOS/Python")
    env = dict(os.environ, CLAUDE_HUMAN_IDENTITY="-", CLAUDE_HUMAN_APP_NAME="Example Agent")

    def build(name, bundle_id):
        r = subprocess.run(["bash", str(PYAPP), str(tmp_path / name)],
                           env=dict(env, CLAUDE_HUMAN_BUNDLE_ID=bundle_id), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    one = build("one.app", "org.example.one")
    two = build("two.app", "org.example.two")
    again = build("again.app", "org.example.one")
    assert _macho_uuid(one) != launcher and _macho_uuid(two) != launcher
    assert _macho_uuid(one) != _macho_uuid(two), "two bundles must not share one either"
    assert _macho_uuid(one) == _macho_uuid(again), "a rebuild keeps the bundle's UUID"
    assert subprocess.run([one, "-c", "import sys; print(sys.version_info[0])"],
                          capture_output=True, text=True).stdout.strip() == "3"
    assert subprocess.run(["codesign", "--verify", "--strict", str(tmp_path / "one.app")]).returncode == 0

    # one built before this rule held the launcher's UUID: it is not current, so it is built again
    raw = bytes.fromhex(_macho_uuid(one).replace("-", ""))
    data = open(one, "rb").read()
    assert data.count(raw) == 1
    open(one, "wb").write(data.replace(raw, bytes.fromhex(launcher.replace("-", ""))))
    subprocess.run(["codesign", "--force", "--sign", "-", str(tmp_path / "one.app")], check=True, capture_output=True)
    assert _macho_uuid(one) == launcher
    build("one.app", "org.example.one")
    assert _macho_uuid(one) != launcher
