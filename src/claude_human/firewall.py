"""The macOS application firewall: whether it is on, and turning it on without cutting off the
programs that must still accept connections.

    from claude_human import firewall
    firewall.on()                                        # True, False, or None when it cannot be read
    firewall.turn_on(keep=("/usr/local/bin/tailscaled",))   # {"ok", "kept", "failed"}

Turning it on blocks incoming connections to every program not allowed, so a program that serves
the network (a VPN daemon, a file server) stops answering unless it is added and unblocked. Both
need `sudo -n`.
"""
from __future__ import annotations

import os
import subprocess

TOOL = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def parse_state(text: str) -> bool | None:
    """`--getglobalstate`'s answer: "Firewall is enabled. (State = 1)" is True, "disabled" False."""
    t = (text or "").lower()
    if "enabled" in t:
        return True
    if "disabled" in t:
        return False
    return None


def on(*, run=subprocess.run) -> bool | None:
    """Whether the firewall is on; None when it cannot be read."""
    try:
        r = run([TOOL, "--getglobalstate"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_state(getattr(r, "stdout", "") or "") if getattr(r, "returncode", 1) == 0 else None


def turn_on(*, keep: tuple[str, ...] = (), run=subprocess.run) -> dict:
    """Turn the firewall on, then add and unblock each program in `keep`.

    {"ok": the firewall reads on afterwards, "kept": the programs added and unblocked, "failed":
    the ones that were not}. A program in `keep` that is missing is left out of both."""
    def sudo(*args) -> bool:
        try:
            r = run(["/usr/bin/sudo", "-n", TOOL, *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return getattr(r, "returncode", 1) == 0
    sudo("--setglobalstate", "on")
    kept, failed = [], []
    for program in keep:
        if not os.path.exists(program):
            continue
        (kept if sudo("--add", program) and sudo("--unblockapp", program) else failed).append(program)
    return {"ok": on(run=run) is True, "kept": kept, "failed": failed}
