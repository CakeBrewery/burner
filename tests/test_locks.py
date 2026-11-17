from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest
from conftest import PY, wait_for

from burner import Host, HostError, paths

SLEEP = [PY, "-c", "import time; time.sleep(60)"]


@pytest.mark.parametrize("terminal", [False, True])
def test_an_ended_session_leaves_no_lock(host: Host, run_dir: Path, terminal: bool) -> None:
    host.start("a", [PY, "-c", "pass"], cwd=run_dir, terminal=terminal)
    wait_for(lambda: host.gone("a"))
    host.start("b", SLEEP, cwd=run_dir, terminal=terminal)
    host.stop("b")
    assert not (run_dir / "a.lock").exists() and not (run_dir / "b.lock").exists()


def test_a_failed_start_leaves_no_new_file(host: Host, run_dir: Path) -> None:
    with pytest.raises(HostError):
        host.start("bad", ["no-such-program-xyz"], cwd=run_dir)
    assert list(run_dir.iterdir()) == []


def test_a_failed_start_keeps_the_last_exit_record_and_log(host: Host, run_dir: Path) -> None:
    host.start("bad", [PY, "-c", "print('first')"], cwd=run_dir)
    wait_for(lambda: host.gone("bad"))
    with pytest.raises(HostError):
        host.start("bad", ["no-such-program-xyz"], cwd=run_dir)
    assert host.exit_status("bad").code == 0
    assert host.tail("bad") == "first"
    assert sorted(p.name for p in run_dir.iterdir()) == ["bad.exit", "bad.log"]


def test_a_probe_does_not_count_an_unlinked_lock(run_dir: Path) -> None:
    lock = run_dir / "x.lock"
    fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    assert paths.lock_probe(lock) is True
    os.unlink(lock)
    assert paths.lock_probe(lock) is False
    os.close(fd)


def test_a_locker_retries_when_the_file_was_replaced(run_dir: Path, monkeypatch) -> None:
    lock = run_dir / "x.lock"
    lock.touch()
    real_flock = fcntl.flock
    calls = []

    def flock(fd: int, op: int) -> None:
        if not calls:
            # The holder unlinks the file between the open and the flock.
            os.unlink(lock)
        calls.append(op)
        real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    fd = paths.lock_file(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert len(calls) == 2
        st = os.stat(lock)
        assert (st.st_dev, st.st_ino) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
    finally:
        os.close(fd)


# Holds a lock of its own for its whole life, and notes when another child holds it.
CHILD = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    with open(sys.argv[2], "a") as f:
        f.write("two children at once\n")
open(os.path.join(sys.argv[3], str(os.getpid())), "w").close()
time.sleep(60)
"""

WORKER = r"""
import json, os, sys, time
from burner import Host, HostError
run_dir, seconds, child = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
host = Host(run_dir, timeout=5)
deadline = time.monotonic() + seconds
started = 0
while time.monotonic() < deadline:
    try:
        host.start("x", [sys.executable, "-c", *child], cwd="/")
    except HostError:
        pass
    else:
        # Count a start only once its child made its own lock attempt.
        try:
            pid = json.load(open(os.path.join(run_dir, "x.json")))["child"]
        except (OSError, ValueError):
            pid = None
        marker = os.path.join(child[-1], str(pid))
        wait = time.monotonic() + 2
        while pid and not os.path.exists(marker) and time.monotonic() < wait:
            time.sleep(0.005)
        started += bool(pid) and os.path.exists(marker)
    try:
        host.stop("x", grace=1.0)
    except HostError:
        pass
    try:
        host.clean("x")
    except HostError:
        pass
print(started)
"""


def test_racing_starts_stops_and_cleans_keep_one_session(
    host: Host, run_dir: Path, tmp_path: Path
) -> None:
    (tmp_path / "markers").mkdir()
    child = [CHILD, str(tmp_path / "child.lock"), str(tmp_path / "overlap"),
             str(tmp_path / "markers")]
    workers = [
        subprocess.Popen([PY, "-c", WORKER, str(run_dir), "5", *child], stdout=subprocess.PIPE)
        for _ in range(8)
    ]
    started = sum(int(w.communicate(timeout=60)[0]) for w in workers)
    assert all(w.returncode == 0 for w in workers)
    host.stop("x")
    host.clean("x")
    assert started > 8
    assert not (tmp_path / "overlap").exists()
    assert list(run_dir.iterdir()) == []
