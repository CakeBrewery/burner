from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest
from conftest import PY, pid_alive, wait_for

from burner import Host, HostError


def meta(run_dir: Path, name: str) -> dict:
    return json.loads((run_dir / f"{name}.json").read_text())


def kill_host(run_dir: Path, name: str) -> dict:
    info = meta(run_dir, name)
    os.kill(info["pid"], signal.SIGKILL)
    wait_for(lambda: not pid_alive(info["pid"]))
    return info


def test_meta_records_the_host_session(host: Host, run_dir: Path) -> None:
    host.start("a", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    info = meta(run_dir, "a")
    assert info["sid"] == os.getsid(info["pid"])


def test_a_silent_child_keeps_its_name_after_a_host_kill(host: Host, run_dir: Path) -> None:
    host.start("quiet", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    info = kill_host(run_dir, "quiet")
    assert host.alive("quiet")
    with pytest.raises(HostError, match="already running"):
        host.start("quiet", [PY, "-c", "pass"], cwd=run_dir)
    host.stop("quiet", grace=1.0)
    assert host.gone("quiet")
    wait_for(lambda: not pid_alive(info["child"]))  # The reap follows the freed name.
    assert host.clean("quiet") == ["quiet"]
    assert list(run_dir.iterdir()) == []


WRITER = "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.05)"


def test_a_writing_child_ends_after_a_host_kill(host: Host, run_dir: Path) -> None:
    host.start("loud", [PY, "-c", WRITER], cwd=run_dir)
    wait_for(lambda: "tick" in host.tail("loud"))
    info = kill_host(run_dir, "loud")
    wait_for(lambda: not pid_alive(info["child"]))
    wait_for(lambda: host.gone("loud"))
    host.clean("loud")


DEAF = (
    "import signal, time\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "print('ready', flush=True)\ntime.sleep(60)"
)


def test_a_terminal_child_that_ignores_hangup_keeps_its_name(host: Host, run_dir: Path) -> None:
    host.start("deaf", [PY, "-c", DEAF], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("deaf"))
    info = kill_host(run_dir, "deaf")
    assert host.alive("deaf")
    host.stop("deaf", grace=1.0)
    assert host.gone("deaf")
    wait_for(lambda: not pid_alive(info["child"]))  # The reap follows the freed name.
    host.clean("deaf")


# The leader forks a survivor that keeps the lock fd, then exits when told to.
LEADER = r"""
import os, signal, sys, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if sys.argv[3] == "leave":
        os.setsid()
    # Tell the test only once the survivor is set up.
    with open(sys.argv[1] + ".tmp", "w") as f:
        f.write(str(os.getpid()))
    os.rename(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(60)
    os._exit(0)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.02)
"""


def start_leader(host: Host, run_dir: Path, how: str) -> tuple[int, dict]:
    pid_file, go = run_dir / "survivor.pid", run_dir / "go"
    host.start("grp", [PY, "-c", LEADER, str(pid_file), str(go), how], cwd=run_dir)
    survivor = int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))
    info = kill_host(run_dir, "grp")
    go.touch()
    wait_for(lambda: not pid_alive(info["child"]))
    return survivor, info


def test_stop_reaches_a_survivor_in_the_group_after_the_leader_exits(
    host: Host, run_dir: Path
) -> None:
    survivor, _ = start_leader(host, run_dir, "stay")
    assert host.alive("grp")
    host.stop("grp", grace=1.0)
    assert host.gone("grp")
    wait_for(lambda: not pid_alive(survivor))  # The reap follows the freed name.
    host.clean("grp")


def test_stop_refuses_a_lock_held_outside_the_group(host: Host, run_dir: Path) -> None:
    survivor, _ = start_leader(host, run_dir, "leave")
    try:
        assert host.alive("grp")
        with pytest.raises(HostError, match="grp.lock"):
            host.stop("grp", grace=0.2)
        assert pid_alive(survivor)
    finally:
        os.kill(survivor, signal.SIGKILL)
    wait_for(lambda: host.gone("grp"))
    host.clean("grp")
