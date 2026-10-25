"""The Wi-Fi networks a Mac has saved, and how each is secured.

macOS keeps every network it has joined in /Library/Preferences/com.apple.wifi.known-networks.plist,
readable only by root, and joins any of them again by itself when it is in range. A saved network
with no security ("Open") is the risky kind: anyone can set up a network of that name and the Mac
joins it. One tap on a café's network adds one.

    from claude_human import wifi
    nets = wifi.known_networks()          # None without passwordless sudo
    open_ones = [n for n in nets if n["open"]]
"""
from __future__ import annotations

import plistlib
import subprocess

KNOWN = "/Library/Preferences/com.apple.wifi.known-networks.plist"
#: the security types macOS writes for a network anyone can join (OWE is "Enhanced Open": encrypted,
#: but anyone may still join it, so it counts as open here)
OPEN_TYPES = ("Open", "OWE", "None")


def parse_known(data: bytes) -> list[dict]:
    """The plist as {ssid, security, open, hidden, added_at}, one per saved network."""
    out = []
    for key, v in (plistlib.loads(data) or {}).items():
        if not isinstance(v, dict):
            continue
        sec = v.get("SupportedSecurityTypes")
        sec = " ".join(sec) if isinstance(sec, list) else (str(sec) if sec is not None else "")
        ssid = v.get("SSID")
        ssid = ssid.decode("utf-8", "replace") if isinstance(ssid, bytes) else (ssid or key)
        out.append({"ssid": ssid, "security": sec,
                    "open": any(t == sec or sec.startswith(t + " ") for t in OPEN_TYPES) or not sec,
                    "hidden": bool(v.get("Hidden")), "added_at": v.get("AddedAt")})
    return out


def known_networks(*, run=subprocess.run) -> list[dict] | None:
    """The saved networks (parse_known), read through `sudo -n defaults export`. None when the file
    cannot be read (no passwordless sudo, not a Mac)."""
    try:
        r = run(["/usr/bin/sudo", "-n", "/usr/bin/defaults", "export", KNOWN, "-"],
                capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0 or not r.stdout:
        return None
    try:
        return parse_known(r.stdout)
    except (plistlib.InvalidFileException, ValueError):
        return None


def forget(ssid: str, *, interface: str = "en0", run=subprocess.run) -> bool:
    """Remove one saved network, so the Mac no longer joins it by itself. True when networksetup
    removed it (through `sudo -n`)."""
    try:
        r = run(["/usr/bin/sudo", "-n", "/usr/sbin/networksetup", "-removepreferredwirelessnetwork",
                 interface, ssid], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(r, "returncode", 1) == 0


def forget_open(*, interface: str = "en0", run=subprocess.run) -> dict:
    """Remove every saved network anyone can join, then read the list again.

    {"forgot": [ssid, ...], "failed": [ssid, ...], "left": the open networks still saved, or None
    when the list could not be read}. Read the list again rather than trust the removals: the count
    afterwards is the fact."""
    nets = known_networks(run=run)
    if nets is None:
        return {"forgot": [], "failed": [], "left": None}
    forgot, failed = [], []
    for n in nets:
        if n["open"]:
            (forgot if forget(n["ssid"], interface=interface, run=run) else failed).append(n["ssid"])
    after = known_networks(run=run)
    return {"forgot": forgot, "failed": failed,
            "left": None if after is None else sum(1 for n in after if n["open"])}
