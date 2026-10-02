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
