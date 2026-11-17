from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import stat
from pathlib import Path

import pytest
from conftest import ECHO, PY, pid_alive, wait_for

from burner import Host

SLEEP = [PY, "-c", "import time; time.sleep(60)"]


def socket_of(run_dir: Path, name: str) -> Path:
    return Path(json.loads((run_dir / f"{name}.json").read_text())["socket"])


def test_each_host_has_its_own_private_socket_dir(long_run_dir: Path) -> None:
    host = Host(long_run_dir, timeout=5)
    host.start("a", SLEEP, cwd="/")
    host.start("b", SLEEP, cwd="/")
    a, b = socket_of(long_run_dir, "a"), socket_of(long_run_dir, "b")
    assert a.parent != b.parent
    for sock in (a, b):
        assert sock.parent.parent == Path("/tmp") and sock.parent.name.startswith("burner-")
        assert stat.S_IMODE(os.lstat(sock.parent).st_mode) == 0o700
        assert stat.S_ISSOCK(os.lstat(sock).st_mode)
    host.stop("a")
    host.stop("b")
    assert not a.parent.exists() and not b.parent.exists()


def test_the_host_holds_a_shared_lock_on_its_socket_dir(long_run_dir: Path) -> None:
    host = Host(long_run_dir, timeout=5)
    host.start("a", SLEEP, cwd="/")
    fd = os.open(socket_of(long_run_dir, "a").parent, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.parametrize("terminal", [False, True])
def test_the_socket_dir_goes_when_the_child_exits(long_run_dir: Path, terminal: bool) -> None:
    host = Host(long_run_dir, timeout=5)
    host.start("a", [PY, "-c", "import time; time.sleep(0.5)"], cwd="/", terminal=terminal)
    sock = socket_of(long_run_dir, "a")
    wait_for(lambda: host.gone("a"))
    wait_for(lambda: not sock.parent.exists())


def test_the_socket_dir_goes_when_the_run_dir_is_deleted(long_run_dir: Path) -> None:
    host = Host(long_run_dir, timeout=5)
    host.start("a", SLEEP, cwd="/")
    sock = socket_of(long_run_dir, "a")
    child = host.sessions()[0].pid
    shutil.rmtree(long_run_dir)
    wait_for(lambda: not pid_alive(child) and not sock.parent.exists(), timeout=15)


def test_next_start_removes_the_socket_dir_of_a_killed_host(long_run_dir: Path) -> None:
    host = Host(long_run_dir, timeout=5)
    host.start("a", [PY, "-c", ECHO], cwd="/", terminal=True)
    wait_for(lambda: "ready" in host.screen("a"))
    meta = json.loads((long_run_dir / "a.json").read_text())
    old = Path(meta["socket"])
    os.kill(meta["pid"], signal.SIGKILL)
    wait_for(lambda: host.gone("a"))
    assert old.parent.exists()
    host.start("a", SLEEP, cwd="/")
    assert not old.parent.exists()


def test_a_removed_socket_dir_is_made_again(long_run_dir: Path) -> None:
    host = Host(long_run_dir, timeout=5)
    host.start("tui", [PY, "-c", ECHO], cwd="/", terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))
    old = socket_of(long_run_dir, "tui")
    shutil.rmtree(old.parent)
    wait_for(lambda: socket_of(long_run_dir, "tui").exists(), timeout=10)
    assert stat.S_IMODE(socket_of(long_run_dir, "tui").parent.stat().st_mode) == 0o700
    assert "ready" in host.screen("tui")
