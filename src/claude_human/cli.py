"""The ``claude-human`` command.

    claude-human build-tools [--out DIR] [--only sckshot|vhid] [--identity ID] [--bundle-id ID]
    claude-human windows
    claude-human screenshot --out FILE [--window ID | --app NAME] [--max-width N]
    claude-human state
    claude-human unlock          (password on stdin)
    claude-human relock
    claude-human use-password
    claude-human approve         (password on stdin)
    claude-human skill [--dir DIR]

A password is read only from stdin (or typed at a hidden prompt when stdin is a terminal). There is
no option that takes one, so it never appears in the process list or the shell history.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, paths

TOOLS = ("sckshot", "vhid")

#: The Claude Code skill that ships inside the package.
SKILL_FILE = Path(__file__).resolve().parent / "skill" / "SKILL.md"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-human",
                                description="Screen capture and lock screen typing for a Mac a person drives remotely.")
    p.add_argument("--version", action="version", version=f"claude-human {__version__}")
    p.add_argument("--sckshot", help="path to the sckshot executable (default: $CLAUDE_HUMAN_SCKSHOT "
                                     "or <bin dir>/sckshot.app/Contents/MacOS/sckshot)")
    p.add_argument("--vhid", help="path to vhid_type (default: $CLAUDE_HUMAN_VHID or <bin dir>/vhid_type)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build-tools", help="compile sckshot.app and vhid_type")
    b.add_argument("--out", help="output folder (default: $CLAUDE_HUMAN_BIN_DIR or ~/.local/share/claude-human/bin)")
    b.add_argument("--only", choices=TOOLS, action="append", help="build only this tool (repeatable)")
    b.add_argument("--identity", help='signing identity for sckshot: "auto" (default), "-" for ad-hoc, '
                                      '"none" to skip signing, or an identity name')
    b.add_argument("--bundle-id", help="bundle identifier for sckshot.app (default local.claude-human.sckshot)")
    b.add_argument("--usage-text", help="the Screen Recording usage text shown by macOS")
    b.add_argument("--pqrs-commit", help="Karabiner-DriverKit-VirtualHIDDevice commit to build vhid_type against")

    sub.add_parser("windows", help="list the ordinary on-screen windows as JSON")

    s = sub.add_parser("screenshot", help="capture the screen or one window with sckshot")
    s.add_argument("--out", required=True, help="output file (.png, or .jpg/.jpeg for JPEG)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--window", type=int, help="CGWindowID to capture")
    g.add_argument("--app", help="capture the largest window of this application")
    s.add_argument("--max-width", type=int, default=0, help="scale down to this width (0 keeps the size)")

    sub.add_parser("state", help="print whether the session is locked and whether a password panel is up")

    u = sub.add_parser("unlock", help="type the password from stdin at the lock screen")
    u.add_argument("--attempts", type=int, default=2, choices=(1, 2), help="tries before giving up (default 2)")
    sub.add_parser("relock", help="sleep the display and wait for the session to lock")
    sub.add_parser("use-password", help="switch a Touch ID panel to its password field (presses Return only)")
    sub.add_parser("approve", help="type the password from stdin into the password panel on screen")

    k = sub.add_parser("skill", help="install the Claude Code skill as <dir>/claude-human/SKILL.md")
    k.add_argument("--dir", default="~/.claude/skills", help="skills folder (default ~/.claude/skills)")
    return p


def install_skill(skills_dir: str | os.PathLike) -> Path:
    """Copy the packaged SKILL.md to <skills_dir>/claude-human/SKILL.md and return that path."""
    target = Path(skills_dir).expanduser() / "claude-human" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SKILL_FILE, target)
    return target


def build_env(args: argparse.Namespace) -> dict:
    """The environment the build scripts read their settings from."""
    env = dict(os.environ)
    for opt, var in (("identity", "CLAUDE_HUMAN_IDENTITY"), ("bundle_id", "CLAUDE_HUMAN_BUNDLE_ID"),
                     ("usage_text", "CLAUDE_HUMAN_USAGE"), ("pqrs_commit", "CLAUDE_HUMAN_PQRS_COMMIT")):
        value = getattr(args, opt, None)
        if value:
            env[var] = value
    return env


def build_commands(args: argparse.Namespace) -> list[list[str]]:
    """The build script invocations for ``build-tools``, in order."""
    out = paths.bin_dir(args.out)
    targets = {"sckshot": out / "sckshot.app", "vhid": out / paths.VHID_NAME}
    return [["bash", str(paths.tool_source(t) / "build.sh"), str(targets[t])]
            for t in TOOLS if not args.only or t in args.only]


def read_password(stream=None) -> str:
    stream = stream or sys.stdin
    if stream.isatty():
        return getpass.getpass("password: ")
    return stream.readline().rstrip("\r\n")


def _report(ok: bool, detail: str) -> int:
    print(("OK " if ok else "FAIL ") + detail)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == "build-tools":
        if sys.platform != "darwin":
            print("build-tools needs macOS (swiftc, clang++, codesign)", file=sys.stderr)
            return 2
        env = build_env(args)
        for c in build_commands(args):
            r = subprocess.run(c, env=env)
            if r.returncode != 0:
                print(f"failed: {' '.join(c)}", file=sys.stderr)
                return r.returncode
        return 0

    if cmd == "skill":
        print(install_skill(args.dir))
        return 0

    if cmd in ("windows", "screenshot", "state"):
        from . import screenshot  # noqa: PLC0415 - needs Quartz
        if cmd == "windows":
            print(json.dumps(screenshot.windows(), indent=2))
            return 0
        if cmd == "state":
            print(json.dumps({"locked": screenshot.screen_locked(),
                              "auth_prompt": screenshot.auth_prompt(),
                              "blocked_by": screenshot.blocked_by()}, indent=2))
            return 0
        wid = screenshot.resolve(args.app, args.window)
        if args.app and wid is None:
            print(f"no window of {args.app!r} is on screen", file=sys.stderr)
            return 1
        try:
            w, h = screenshot.save(Path(args.out), wid, max_width=args.max_width, sckshot=args.sckshot)
        except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"{args.out} {w}x{h}")
        return 0

    from . import unlock  # noqa: PLC0415
    if cmd == "unlock":
        return _report(*unlock.unlock(read_password(), helper=args.vhid, attempts=args.attempts))
    if cmd == "relock":
        return _report(*unlock.relock())
    if cmd == "use-password":
        return _report(*unlock.use_password(helper=args.vhid))
    if cmd == "approve":
        return _report(*unlock.approve(read_password(), helper=args.vhid))
    return 2


if __name__ == "__main__":
    sys.exit(main())
