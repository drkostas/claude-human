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
    claude-human station [--port N] [--app NAME] [--token-file FILE] [--fps N]
    claude-human prepare [--phase place|input|all] APP STEP...
    claude-human notify [--url URL] [--title T] [--priority P] [--tag T] [--link URL] [--file F] MESSAGE

A password is read only from stdin (or typed at a hidden prompt when stdin is a terminal). There is
no option that takes one, so it never appears in the process list or the shell history. The same
holds for the ntfy token and password, which ``notify`` reads from the environment only.
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

    st = sub.add_parser("station", help="serve one window (or the display) to a phone, with input")
    st.add_argument("--port", type=int, default=8789, help="port on 127.0.0.1 (default 8789, 0 for a free one)")
    st.add_argument("--app", help="pin the token to this application's window (it then cannot list or choose windows)")
    st.add_argument("--token-file", help="file holding the token (default: $CLAUDE_HUMAN_STATION_TOKEN_FILE or "
                                         "~/.config/claude-human/station-token, made with mode 0600 if missing). "
                                         "$CLAUDE_HUMAN_STATION_TOKEN wins over any file")
    st.add_argument("--fps", type=float, default=8.0, help="default frames per second of a stream")

    pr = sub.add_parser("prepare", help="run a window recipe (open, wait, search, click...) before handing a window over")
    pr.add_argument("--phase", choices=("place", "input", "all"), default="all",
                    help="place runs open and wait only, input runs the rest, all runs both (default)")
    pr.add_argument("app", help="the application the recipe is for")
    pr.add_argument("steps", nargs="*", help='steps such as "open x-apple.systempreferences:..." "wait 1" "search Login Items"')

    n = sub.add_parser("notify", help="send a message to a person through an ntfy topic")
    n.add_argument("message", help='the message text ("-" reads it from stdin)')
    n.add_argument("--url", help="the topic URL, https://host/topic (default: $CLAUDE_HUMAN_NTFY_URL)")
    n.add_argument("--title", default="", help="the title line")
    n.add_argument("--priority", help="min, low, default, high, max or 1 to 5")
    n.add_argument("--tag", action="append", default=[], help="a tag or emoji short code (repeatable)")
    n.add_argument("--link", help="where a tap on the message lands")
    n.add_argument("--attach", help="a URL to a picture shown with the message")
    n.add_argument("--token-env", default="CLAUDE_HUMAN_NTFY_TOKEN",
                   help="the environment variable that holds the access token (default "
                        "CLAUDE_HUMAN_NTFY_TOKEN). Basic auth reads CLAUDE_HUMAN_NTFY_USER and "
                        "CLAUDE_HUMAN_NTFY_PASSWORD")
    n.add_argument("--timeout", type=float, default=10.0, help="seconds before giving up (default 10)")
    n.add_argument("--file", help="also the floor: append the message to this file when the send fails")

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


def station_auth(args: argparse.Namespace):
    """The TokenAuth for ``claude-human station``, and where its token came from (never the token)."""
    from .station import auth  # noqa: PLC0415
    token = os.environ.get(auth.ENV_TOKEN, "").strip()
    if token:
        return auth.TokenAuth(token, app=args.app), f"${auth.ENV_TOKEN}"
    path = args.token_file or os.environ.get(auth.ENV_TOKEN_FILE) or None
    _tok, where = auth.load_or_create_token(path)
    return auth.TokenAuth(token_file=where, app=args.app), str(where)


def serve_station(args: argparse.Namespace) -> int:
    from .station import make_server  # noqa: PLC0415
    a, where = station_auth(args)
    srv = make_server(a, port=args.port, fps=args.fps)
    port = srv.server_address[1]
    print(f"station on http://127.0.0.1:{port}/view (token from {where}"
          f"{', pinned to ' + args.app if args.app else ''})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def send_notification(args: argparse.Namespace, stdin=None) -> int:
    from . import notify  # noqa: PLC0415
    text = (stdin or sys.stdin).read() if args.message == "-" else args.message
    try:
        n = notify.from_env(args.url, token_env=args.token_env, timeout=args.timeout)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    ok, detail = n.notify(args.title, text, priority=args.priority, link=args.link,
                          tags=args.tag, attach=args.attach)
    if not ok and args.file:
        fok, fdetail = notify.FileNotifier(args.file).notify(args.title, text, priority=args.priority,
                                                             link=args.link, tags=args.tag)
        detail += f"; file: {fdetail}"
    return _report(ok, detail)


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

    if cmd == "station":
        return serve_station(args)

    if cmd == "notify":
        return send_notification(args)

    if cmd == "prepare":
        from .station import prepare  # noqa: PLC0415
        log = prepare.run(args.app, args.steps, phase=args.phase)
        for line in log:
            print(line)
        return 0 if prepare.succeeded(log) else 1

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
