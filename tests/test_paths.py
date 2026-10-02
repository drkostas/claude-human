from pathlib import Path

import pytest

from claude_human import paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in (paths.ENV_BIN_DIR, paths.ENV_SCKSHOT, paths.ENV_VHID):
        monkeypatch.delenv(v, raising=False)


def test_default_bin_dir_is_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.bin_dir() == tmp_path / ".local" / "share" / "claude-human" / "bin"


def test_bin_dir_from_env_then_argument(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_BIN_DIR, str(tmp_path / "env"))
    assert paths.bin_dir() == tmp_path / "env"
    assert paths.bin_dir(tmp_path / "arg") == tmp_path / "arg"


def test_helpers_follow_the_bin_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_BIN_DIR, str(tmp_path))
    assert paths.sckshot_path() == tmp_path / "sckshot.app" / "Contents" / "MacOS" / "sckshot"
    assert paths.vhid_path() == tmp_path / "vhid_type"


def test_helper_env_overrides_bin_dir_and_argument_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_BIN_DIR, str(tmp_path))
    monkeypatch.setenv(paths.ENV_SCKSHOT, "/opt/x/sckshot")
    monkeypatch.setenv(paths.ENV_VHID, "/opt/x/vhid")
    assert paths.sckshot_path() == Path("/opt/x/sckshot")
    assert paths.vhid_path() == Path("/opt/x/vhid")
    assert paths.sckshot_path("/a/b") == Path("/a/b")
    assert paths.vhid_path("/a/c") == Path("/a/c")


def test_tool_sources_ship_with_the_package():
    sck = paths.tool_source("sckshot")
    vhid = paths.tool_source("vhid")
    assert (sck / "sckshot.swift").is_file() and (sck / "build.sh").is_file()
    assert (vhid / "type_string.cpp").is_file() and (vhid / "build.sh").is_file()
    with pytest.raises(ValueError):
        paths.tool_source("nope")
