"""Process facts: alive, the peer of a connection, parents, and memory footprint."""
import os
import sys

import pytest

from claude_human import process


def test_alive_dead_and_unknown():
    assert process.process_alive(os.getpid()) is True
    assert process.process_alive(None) is None
    assert process.process_alive("not a pid") is None
    assert process.process_alive(2 ** 22 + 12345) is False


def test_peer_is_the_one_other_process_on_the_connection():
    class R:
        stdout = f"p{os.getpid()}\np4242\n"
    assert process.peer_pid("127.0.0.1", 8787, run=lambda *a, **k: R()) == 4242
    class Two:
        stdout = "p11\np12\n"
    assert process.peer_pid("127.0.0.1", 8787, run=lambda *a, **k: Two()) is None


def test_ancestors_walk_up_and_stop_at_a_loop():
    procs = {5: {"ppid": 4}, 4: {"ppid": 1}, 1: {"ppid": 0}, 7: {"ppid": 8}, 8: {"ppid": 7}}
    assert process.ancestors(5, procs) == [5, 4, 1]
    assert process.ancestors(7, procs) == [7, 8]


@pytest.mark.skipif(sys.platform != "darwin", reason="proc_pid_rusage is macOS")
def test_footprint_of_this_process():
    gb = process.footprint_gb(os.getpid())
    assert gb is not None and 0 < gb < 64
    assert process.total_gb([os.getpid(), 2 ** 22 + 12345]) == pytest.approx(gb, rel=0.5)
