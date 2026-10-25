"""AirPlay Receiver on a Mac: whether it listens, and turning it off.

ControlCenter runs AirPlay Receiver and listens on ports 7000 (AirPlay) and 5000 (its audio) on
every interface, so anyone on the network can reach it. The setting lives in the user's
com.apple.airplay.security domain as ReceiverDisabled (true is off), and ControlCenter reads it
when it starts or when it is told the preferences changed. The setting can read off while the
ports still answer, so the ports are the fact and the setting only the way to change it.

    from claude_human import airplay
    airplay.listening()        # True, False, or None when lsof cannot run
    airplay.receiver_off()     # {"ok": bool, "listening": bool | None}
"""
from __future__ import annotations

import subprocess
import time

from claude_human import process

DOMAIN = "com.apple.airplay.security"
KEY = "ReceiverDisabled"
#: the ports AirPlay Receiver listens on: 7000 for AirPlay, 5000 for its audio
PORTS = (5000, 7000)
#: an address that means every interface
EVERY = ("*", "0.0.0.0", "::")
NOTIFY = "com.apple.airplay.prefsChanged"
#: how long to wait for the ports to close after ControlCenter restarts, in seconds
WAIT_S = 20.0


def listening(*, run=subprocess.run) -> bool | None:
    """Whether something listens on an AirPlay port on every interface; None when lsof cannot run."""
    sockets = process.listening(run=run)
    if sockets is None:
        return None
    return any(s["port"] in PORTS and s["address"] in EVERY for s in sockets)


def receiver_off(*, run=subprocess.run, sleep=time.sleep, wait_s: float = WAIT_S) -> dict:
    """Turn AirPlay Receiver off and wait for its ports to close.

    Writes ReceiverDisabled, tells ControlCenter the preferences changed, and restarts ControlCenter
    (macOS starts it again at once). {"ok": True} only once nothing listens on the ports any more;
    {"ok": False, "listening": ...} when they still answer after `wait_s`, or cannot be read."""
    run(["/usr/bin/defaults", "write", DOMAIN, KEY, "-bool", "true"], capture_output=True, text=True, timeout=20)
    run(["/usr/bin/notifyutil", "-p", NOTIFY], capture_output=True, text=True, timeout=20)
    run(["/usr/bin/killall", "ControlCenter"], capture_output=True, text=True, timeout=20)
    end = time.monotonic() + wait_s
    while True:
        now = listening(run=run)
        if now is False:
            return {"ok": True, "listening": False}
        if time.monotonic() >= end:
            return {"ok": False, "listening": now}
        sleep(1.0)
