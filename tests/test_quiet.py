from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest
from conftest import PY, wait_for

from burner import Host
from burner import client as client_module
from burner import host as host_module

FACTOR = float(os.environ.get("BURNER_BENCH_FACTOR", "1"))


def test_the_loop_sleeps_until_the_next_thing_to_do(run_dir: Path) -> None:
    h = host_module.SessionHost(run_dir, "bare", {"terminal": True}, 0o022)
    now = time.monotonic()
    assert h.check_seconds == host_module.FILE_CHECK_SECONDS
    h.next_check = now + host_module.FILE_CHECK_SECONDS
    assert h._timeout() == pytest.approx(host_module.FILE_CHECK_SECONDS, abs=0.1)
    ours, peer = socket.socketpair()
    with ours, peer:
        c = host_module.Client(ours)
        h.clients[c] = None
        c.last = now - host_module.IDLE_CLIENT_SECONDS + 2
        assert h._timeout() == pytest.approx(2, abs=0.1)
        h.attached = c  # An attached client is never idle.
        assert h._timeout() == pytest.approx(host_module.FILE_CHECK_SECONDS, abs=0.1)
    h.timers = {9: now + 1}
    assert h._timeout() == pytest.approx(1, abs=0.1)


def test_the_end_of_the_child_is_seen_at_once(host: Host, run_dir: Path) -> None:
    host.start("short", [PY, "-c", "import time; time.sleep(1)"], cwd=run_dir)
    started = time.monotonic()
    wait_for(lambda: host.gone("short"), interval=0.01)
    assert time.monotonic() - started < 1.5


def test_wait_gone_polls_often(host: Host, run_dir: Path, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    calls = iter([False, False, True])
    monkeypatch.setattr(Host, "gone", lambda self, name: next(calls))
    assert host._wait_gone("x", 5.0)
    assert sleeps == [0.01, 0.01]


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_host_at_rest_rarely_wakes(host: Host, run_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_FILE_CHECK_SECONDS", None)
    host.start("rest", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    pid = json.loads((run_dir / "rest.json").read_text())["pid"]

    def switches() -> int:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("voluntary_ctxt_switches:"):
                return int(line.split()[1])
        raise AssertionError("no voluntary_ctxt_switches")

    time.sleep(1)
    before = switches()
    time.sleep(5)
    assert switches() - before <= 0.5 * 5 * FACTOR
