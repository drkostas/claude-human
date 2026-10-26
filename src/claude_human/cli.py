"""The ``claude-human`` command.

    claude-human build-tools [--out DIR] [--only sckshot|vhid] [--identity ID] [--bundle-id ID]
    claude-human python-app OUT.app --bundle-id ID [--name NAME] [--exe NAME] [--python PY] [--identity ID]
    claude-human windows
    claude-human screenshot --out FILE [--window ID | --app NAME] [--max-width N]
    claude-human state
    claude-human unlock          (password on stdin)
    claude-human relock
    claude-human use-password
    claude-human approve         (password on stdin)
    claude-human consent [list [--out PNG] | allow | deny]
    claude-human skill [--dir DIR]
    claude-human station [--port N] [--app NAME] [--token-file FILE] [--fps N]
    claude-human key --window ID COMBO... [--idle S] [--wait S] [--no-bring] [--no-restore] [--json]
    claude-human prepare [--phase place|input|all] APP STEP...
    claude-human notify [--url URL] [--title T] [--priority P] [--tag T] [--link URL] [--file F] MESSAGE
    claude-human task open CAPABILITY SUBJECT --reason R [--steps S] [--handoff KIND=TARGET] [-- VERIFY ARGS...]
    claude-human task list | verify [ID] | withdraw ID | comment ID TEXT | history | serve [--port N]

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

    pa = sub.add_parser("python-app", help="build a signed .app that runs Python, so a program keeps "
                                           "its macOS grants across Python upgrades; prints its executable")
    pa.add_argument("out", help="the .app to build (rebuilt only when it is not current)")
    pa.add_argument("--bundle-id", required=True, help="CFBundleIdentifier; grants are held against it")
    pa.add_argument("--name", help="the name macOS shows in Privacy & Security (default: the .app's name)")
    pa.add_argument("--exe", help="the executable's name (default: the name in lowercase, with dashes)")
    pa.add_argument("--python", help="the Python to run (default /opt/homebrew/bin/python3)")
    pa.add_argument("--identity", help='"auto" (default), "-" for ad-hoc, or an identity name')

    sub.add_parser("windows", help="list the ordinary on-screen windows as JSON")

    sub.add_parser("brave", help="run JavaScript on a page in the person's signed-in browser "
                                 "(brave URL JS_FILE [--keep-open] [--window ID] [--key KEY] ...), "
                                 "or whether it has a saved login (brave has-login SITE)")

    sub.add_parser("key", help="press key combinations in one window through the station, once the "
                               "Mac has been idle (key --window ID COMBO... [--idle S] [--json])")

    sub.add_parser("android", help="tap an Android app over adb only once its window has focus, no "
                                   "banner or call shows, the phone is idle and the screen around the "
                                   "point is still, then read the screen back "
                                   "(android tap --serial S --package P X Y [--json] [--out PNG]); or list, "
                                   "pull and lay out the new files in a phone folder, read only "
                                   "(android files --serial S --since TIME [--pull DIR] [--sheet PNG]); "
                                   "pinch with two fingers behind the same checks "
                                   "(android pinch --serial S --package P X Y --in|--out [--slow]); or open "
                                   "a page in the phone's Chromium browser and run a script in it "
                                   "(android browser --serial S --package P [--open URL] [--url U --eval JS])")

    s = sub.add_parser("screenshot", help="capture the screen or one window with sckshot")
    s.add_argument("--out", required=True, help="output file (.png, or .jpg/.jpeg for JPEG)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--window", type=int, help="CGWindowID to capture")
    g.add_argument("--app", help="capture the largest window of this application")
    s.add_argument("--max-width", type=int, default=0, help="scale down to this width (0 keeps the size)")

    sub.add_parser("state", help="print whether the session is locked and whether a password panel is up")
    sub.add_parser("unattended", help="print whether the Mac comes back to the desktop by itself after a restart: FileVault, automatic login, the stored password")

    u = sub.add_parser("unlock", help="type the password from stdin at the lock screen")
    u.add_argument("--attempts", type=int, default=2, choices=(1, 2), help="tries before giving up (default 2)")
    sub.add_parser("relock", help="sleep the display and wait for the session to lock")
    sub.add_parser("use-password", help="switch a Touch ID panel to its password field (presses Return only)")
    sub.add_parser("approve", help="type the password from stdin into the password panel on screen")
    cs = sub.add_parser("consent", help="list the macOS consent prompts on screen, or answer the front one")
    cs.add_argument("action", nargs="?", default="list", choices=("list", "allow", "deny"))
    cs.add_argument("--out", help="with list: save a picture of the first prompt here (PNG)")

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

    t = sub.add_parser("task", help="ask a person to do something and close it only on an observation")
    t.add_argument("--db", help="the task file (default: $CLAUDE_HUMAN_TASKS_DB or "
                                "~/.local/share/claude-human/tasks.db)")
    ts = t.add_subparsers(dest="task_command", required=True)
    to = ts.add_parser("open", help="open a task, or join the open one for the same capability and subject")
    to.epilog = "After --, the command that exits 0 only when the task is done, as separate arguments."
    to.add_argument("capability", help="what the person does, such as approve, sign-in, plug-in")
    to.add_argument("subject", help="what it is about, such as app://backup")
    to.add_argument("--reason", required=True, help="why the person is asked")
    to.add_argument("--steps", help="the words a person follows, the floor of every chain")
    to.add_argument("--owner", help="who raised it (a chat, a program)")
    to.add_argument("--actor", help="who asks, for the record")
    to.add_argument("--tier", default="confirm", help="how carefully to act (default confirm)")
    to.add_argument("--handoff", action="append", default=[], metavar="KIND=TARGET",
                    help="a way to reach the thing, such as url=https://... (repeatable, best first)")
    to.add_argument("--link", help="what a tap on the notification opens, with {id} for the task "
                                   "(default: $CLAUDE_HUMAN_TASK_LINK)")
    to.add_argument("--ntfy-url", help="tell the person through this ntfy topic "
                                       "(default: $CLAUDE_HUMAN_NTFY_URL, none when unset)")
    tl = ts.add_parser("list", help="the open tasks as JSON")
    tl.add_argument("--supports", default="steps,url", help="the kinds the reader can show (default steps,url)")
    tl.add_argument("--platform", default="any", help="where the reader runs (default any)")
    tv = ts.add_parser("verify", help="run the verify command of open tasks and close the done ones")
    tv.add_argument("id", nargs="?", help="only this task (exit 1 when it is not done)")
    tw = ts.add_parser("withdraw", help="drop an open task")
    tw.add_argument("id")
    tw.add_argument("--reason")
    tc = ts.add_parser("comment", help="record what a person said about a task")
    tc.add_argument("id")
    tc.add_argument("text")
    th = ts.add_parser("history", help="what happened, newest first")
    th.add_argument("--limit", type=int, default=50)
    tsv = ts.add_parser("serve", help="serve the tasks over HTTP on 127.0.0.1 with a bearer token")
    tsv.add_argument("--port", type=int, default=8790, help="port on 127.0.0.1 (default 8790, 0 for a free one)")
    tsv.add_argument("--token-file", help="file holding the token (default: $CLAUDE_HUMAN_TASKS_TOKEN_FILE "
                                          "or ~/.config/claude-human/tasks-token, made with mode 0600 "
                                          "if missing). $CLAUDE_HUMAN_TASKS_TOKEN wins over any file")

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


def python_app_command(args: argparse.Namespace) -> list[str]:
    """The build script invocation for ``python-app``."""
    return ["bash", str(paths.tool_source("pyapp") / "build.sh"), str(args.out)]


def python_app_env(args: argparse.Namespace) -> dict[str, str]:
    """The environment ``python-app`` passes to its build script."""
    env = dict(os.environ, CLAUDE_HUMAN_BUNDLE_ID=args.bundle_id)
    for opt, var in (("name", "CLAUDE_HUMAN_APP_NAME"), ("exe", "CLAUDE_HUMAN_EXE"),
                     ("python", "CLAUDE_HUMAN_PYTHON"), ("identity", "CLAUDE_HUMAN_IDENTITY")):
        value = getattr(args, opt, None)
        if value:
            env[var] = value
    return env


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
    from .serving import serve_until_stopped  # noqa: PLC0415
    serve_until_stopped(srv, log=lambda m: print(m, flush=True))
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


def parse_handoff(text: str, preference: int):
    from .tasks import Handoff  # noqa: PLC0415
    kind, sep, target = text.partition("=")
    if not sep or not kind or not target:
        raise ValueError(f"a handoff is KIND=TARGET, not {text!r}")
    return Handoff(kind.strip(), target, preference=preference)


def run_task(args: argparse.Namespace) -> int:
    from . import notify, tasks  # noqa: PLC0415
    store = tasks.SqliteTaskStore(args.db)
    tc = args.task_command
    if tc == "open":
        verify = list(getattr(args, "verify", None) or [])
        try:
            handoffs = [parse_handoff(h, (i + 1) * 10) for i, h in enumerate(args.handoff)]
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        url = args.ntfy_url or os.environ.get(notify.ENV_URL)
        notifier = notify.from_env(url) if url else None
        link = args.link or os.environ.get("CLAUDE_HUMAN_TASK_LINK")
        engine = tasks.TaskEngine(store, notifier=notifier, link=link)
        tid, new = engine.request(args.capability, args.subject, args.reason, owner=args.owner,
                                  actor=args.actor, steps=args.steps, verify=verify or None,
                                  handoffs=handoffs, tier=args.tier)
        print(json.dumps({"id": tid, "new": new}))
        return 0
    engine = tasks.TaskEngine(store)
    if tc == "list":
        sup = [x for x in args.supports.split(",") if x] or ["steps"]
        print(json.dumps(engine.pending_cards(sup, args.platform), indent=2, default=str))
        return 0
    if tc == "verify":
        closed = engine.verify_pending(only=args.id)
        for t in closed:
            print(f"done {t.id} {t.capability} {t.subject}")
        if args.id:
            return 0 if closed else 1
        return 0
    if tc == "withdraw":
        out = store.withdraw(args.id, os.environ.get("USER") or None, args.reason)
        print(json.dumps(out))
        return 0 if out.get("ok") else 1
    if tc == "comment":
        out = store.comment(args.id, args.text, os.environ.get("USER") or None)
        print(json.dumps(out))
        return 0 if out.get("ok") else 1
    if tc == "history":
        print(json.dumps(store.history(args.limit), indent=2, default=str))
        return 0
    if tc == "serve":
        from .tasks import server  # noqa: PLC0415
        token = os.environ.get(server.ENV_TOKEN, "").strip()
        if token:
            srv, where = server.make_server(engine, token=token, port=args.port), f"${server.ENV_TOKEN}"
        else:
            _tok, path = server.load_or_create_token(args.token_file)
            srv, where = server.make_server(engine, token_file=path, port=args.port), str(path)
        print(f"tasks on http://127.0.0.1:{srv.server_address[1]} (token from {where}, "
              f"store {store.path})", flush=True)
        from .serving import serve_until_stopped  # noqa: PLC0415
        serve_until_stopped(srv, log=lambda m: print(m, flush=True))
        return 0
    return 2


def read_password(stream=None) -> str:
    stream = stream or sys.stdin
    if stream.isatty():
        return getpass.getpass("password: ")
    return stream.readline().rstrip("\r\n")


def _report(ok: bool, detail: str) -> int:
    print(("OK " if ok else "FAIL ") + detail)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["android"]:
        from claude_human import android   # noqa: PLC0415
        return android.main(argv[1:])
    if argv[:1] == ["key"]:
        from claude_human import keys   # noqa: PLC0415
        return keys.main(argv[1:])
    if argv[:1] == ["brave"]:
        # its own arguments, passed through as brave.main reads them
        from claude_human import brave   # noqa: PLC0415
        return brave.main(argv[1:])
    # `task open ... -- CMD ARGS` carries the verify command as separate arguments, so it is never
    # a shell string. Everything after the first -- is that command.
    verify: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, verify = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.verify = verify
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

    if cmd == "python-app":
        if sys.platform != "darwin":
            print("python-app needs macOS (codesign, install_name_tool)", file=sys.stderr)
            return 2
        r = subprocess.run(python_app_command(args), env=python_app_env(args),
                           capture_output=True, text=True)
        sys.stderr.write(r.stderr)
        if r.returncode == 0:
            print(r.stdout.strip())
        return r.returncode

    if cmd == "skill":
        print(install_skill(args.dir))
        return 0

    if cmd == "station":
        return serve_station(args)

    if cmd == "notify":
        return send_notification(args)

    if cmd == "task":
        return run_task(args)

    if cmd == "prepare":
        from .station import prepare  # noqa: PLC0415
        log = prepare.run(args.app, args.steps, phase=args.phase)
        for line in log:
            print(line)
        return 0 if prepare.succeeded(log) else 1

    if cmd == "unattended":
        from . import unattended  # noqa: PLC0415
        print(json.dumps(unattended.read(), indent=2))
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

    if cmd == "consent":
        from . import consent  # noqa: PLC0415
        if args.action != "list":
            return _report(*consent.answer(args.action, helper=args.vhid))
        found = consent.prompts()
        if found and args.out and consent.capture(found[0], args.out):
            found[0]["image"] = args.out
            found[0]["text"] = consent.read_text(args.out)
        print(json.dumps(found, indent=2))
        return 0 if found is not None else 1

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
