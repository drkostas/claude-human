"""The saved Wi-Fi networks and how each is secured."""
import plistlib

from claude_human import wifi

PLIST = plistlib.dumps({
    "wifi.network.ssid.Home": {"SSID": b"Home", "SupportedSecurityTypes": "WPA2/WPA3 Personal", "AddedAt": 1},
    "wifi.network.ssid.Cafe": {"SSID": b"Cafe", "SupportedSecurityTypes": "Open", "Hidden": False},
    "wifi.network.ssid.Airport": {"SSID": b"Airport", "SupportedSecurityTypes": "OWE"},
    "wifi.network.ssid.Work": {"SSID": b"Work", "SupportedSecurityTypes": "WPA2 Enterprise"},
})


def test_parse_known_marks_the_open_ones():
    got = {n["ssid"]: n for n in wifi.parse_known(PLIST)}
    assert {s for s, n in got.items() if n["open"]} == {"Cafe", "Airport"}
    assert got["Home"]["security"] == "WPA2/WPA3 Personal" and not got["Work"]["open"]


def test_known_networks_reads_through_sudo_and_none_when_refused():
    seen = []
    class R:
        returncode, stdout = 0, PLIST
    nets = wifi.known_networks(run=lambda argv, **k: seen.append(argv) or R())
    assert len(nets) == 4 and seen[0][:4] == ["/usr/bin/sudo", "-n", "/usr/bin/defaults", "export"]
    class Refused:
        returncode, stdout = 1, b""
    assert wifi.known_networks(run=lambda *a, **k: Refused()) is None
