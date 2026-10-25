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


def test_forget_open_removes_each_open_network_and_reads_the_count_again():
    seen, removed = [], set()

    def run(argv, **k):
        seen.append(argv)
        class R:
            returncode, stdout = 0, b""
        if "export" in argv:
            nets = {k_: v for k_, v in plistlib.loads(PLIST).items() if v["SSID"].decode() not in removed}
            R.stdout = plistlib.dumps(nets)
        elif "-removepreferredwirelessnetwork" in argv:
            removed.add(argv[-1])
            R.returncode = 0 if argv[-1] == "Cafe" else 1
            if argv[-1] != "Cafe":
                removed.discard(argv[-1])
        return R()
    out = wifi.forget_open(run=run)
    assert out == {"forgot": ["Cafe"], "failed": ["Airport"], "left": 1}
    assert all(a[:3] == ["/usr/bin/sudo", "-n", "/usr/sbin/networksetup"] for a in seen
               if "-removepreferredwirelessnetwork" in a)


def test_forget_open_with_no_readable_list_removes_nothing():
    class Refused:
        returncode, stdout = 1, b""
    assert wifi.forget_open(run=lambda *a, **k: Refused()) == {"forgot": [], "failed": [], "left": None}
