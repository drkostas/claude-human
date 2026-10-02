"""Where the compiled helpers live, and where their sources ship inside the package.

Each path is resolved in this order: an explicit argument, an environment variable, then a default
under the bin directory. The bin directory is ``CLAUDE_HUMAN_BIN_DIR`` or
``~/.local/share/claude-human/bin``.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_BIN_DIR = "CLAUDE_HUMAN_BIN_DIR"
ENV_SCKSHOT = "CLAUDE_HUMAN_SCKSHOT"
ENV_VHID = "CLAUDE_HUMAN_VHID"

SCKSHOT_IN_APP = Path("sckshot.app") / "Contents" / "MacOS" / "sckshot"
VHID_NAME = "vhid_type"

TOOLS = Path(__file__).resolve().parent / "tools"


def bin_dir(override: str | os.PathLike | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_BIN_DIR)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "claude-human" / "bin"


def sckshot_path(override: str | os.PathLike | None = None) -> Path:
    """The sckshot executable inside its .app bundle."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_SCKSHOT)
    if env:
        return Path(env).expanduser()
    return bin_dir() / SCKSHOT_IN_APP


def vhid_path(override: str | os.PathLike | None = None) -> Path:
    """The vhid_type executable."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get(ENV_VHID)
    if env:
        return Path(env).expanduser()
    return bin_dir() / VHID_NAME


def tool_source(name: str) -> Path:
    """The folder holding a helper's source and build script ("sckshot" or "vhid")."""
    path = TOOLS / name
    if not (path / "build.sh").is_file():
        raise ValueError(f"no build script for {name!r} in {TOOLS}")
    return path
