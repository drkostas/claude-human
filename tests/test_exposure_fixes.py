"""AirPlay Receiver off and the firewall on: each act is checked afterwards, never assumed."""
from claude_human import airplay, firewall

LISTEN = "p101\ncControlCenter\nLuser\nn*:7000\nn*:5000\n"
QUIET = "p202\ncsshd\nLroot\nn127.0.0.1:22\n"


class R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_receiver_off_writes_the_setting_restarts_controlcenter_and_waits_for_the_ports():
    seen, reads = [], [LISTEN, QUIET]

    def run(argv, **k):
        seen.append(argv)
        return R(0, reads.pop(0) if argv[0].endswith("lsof") else "")
    out = airplay.receiver_off(run=run, sleep=lambda s: None)
    assert out == {"ok": True, "listening": False}
    assert seen[0] == ["/usr/bin/defaults", "write", "com.apple.airplay.security", "ReceiverDisabled", "-bool", "true"]
    assert seen[1][:2] == ["/usr/bin/notifyutil", "-p"] and seen[2] == ["/usr/bin/killall", "ControlCenter"]


def test_receiver_off_is_not_ok_while_the_ports_still_answer():
    out = airplay.receiver_off(run=lambda argv, **k: R(0, LISTEN if argv[0].endswith("lsof") else ""),
                               sleep=lambda s: None, wait_s=0)
    assert out == {"ok": False, "listening": True}


def test_the_firewall_state_is_read_from_its_own_words():
    assert firewall.parse_state("Firewall is enabled. (State = 1)") is True
    assert firewall.parse_state("Firewall is disabled. (State = 0)") is False
    assert firewall.parse_state("") is None


def test_turn_on_keeps_the_named_programs_allowed_and_reads_the_state_after(tmp_path):
    daemon = tmp_path / "vpnd"
    daemon.write_text("")
    seen, state = [], ["Firewall is disabled. (State = 0)"]

    def run(argv, **k):
        seen.append(argv)
        if "--setglobalstate" in argv:
            state[0] = "Firewall is enabled. (State = 1)"
        return R(0, state[0] if "--getglobalstate" in argv else "")
    out = firewall.turn_on(keep=(str(daemon), str(tmp_path / "missing")), run=run)
    assert out == {"ok": True, "kept": [str(daemon)], "failed": []}
    sudo = [a[3:] for a in seen if a[:2] == ["/usr/bin/sudo", "-n"]]
    assert sudo == [["--setglobalstate", "on"], ["--add", str(daemon)], ["--unblockapp", str(daemon)]]
