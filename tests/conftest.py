from __future__ import annotations

import contextlib
import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from burner import Host, paths
from burner import client as client_module

PY = sys.executable

# Echoes every read as hex, in raw mode, so tests can see the exact bytes.
# Reports its size on SIGWINCH and exits with code 7 on "q".
ECHO = r"""
import os, signal, sys, tty
def report(*_):
    cols, rows = os.get_terminal_size(0)
    os.write(1, b"size:%dx%d\r\n" % (cols, rows))
signal.signal(signal.SIGWINCH, report)
tty.setraw(0)
if len(sys.argv) > 1:
    os.write(1, sys.argv[1].encode())
os.write(1, b"ready\r\n")
while True:
    data = os.read(0, 1024)
    if not data or data == b"q":
        sys.exit(7)
    os.write(1, b"got:" + data.hex().encode() + b"\r\n")
"""

# Ignores the signals a polite stop sends.
STUBBORN = r"""
import signal, time
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("stubborn", flush=True)
while True:
    time.sleep(1)
"""


def wait_for(check: Callable[[], object], timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# The leak guard reports what a test leaves behind as warnings while False,
# and fails the test while True.
STRICT_LEAK_GUARD = True
# Files an ended session must not leave in its run dir.
LIVE_SUFFIXES = (".lock", ".json", ".sock")
HOST_EXIT_WAIT = 8.0
# Hosts that tests start check their files this often, so tests need not wait 5 s.
TEST_FILE_CHECK_SECONDS = 0.5


class LeakWarning(UserWarning):
    """A test left a session's files or processes behind."""


# (run dir, name, socket path) of each session the current test started.
_started: list[tuple[Path, str, str | None]] = []


def _report(problems: list[str]) -> None:
    for problem in problems:
        if STRICT_LEAK_GUARD:
            pytest.fail(f"leak: {problem}")
        warnings.warn(f"leak: {problem}", LeakWarning, stacklevel=2)


def _leftover_files(run_dir: Path) -> list[str]:
    problems = []
    for name in sorted({n for d, n, _ in _started if d == run_dir}):
        temp = re.compile(re.escape(f".{name}.") + r"(json|exit)\.\d+\.tmp")
        for path in sorted(run_dir.iterdir()) if run_dir.is_dir() else []:
            if path.name in {name + s for s in LIVE_SUFFIXES} or temp.fullmatch(path.name):
                problems.append(f"{path} remains")
    return problems


# Kept apart, so a test that replaces subprocess.Popen does not blind the guard.
_POPEN = subprocess.Popen


def _host_pids(run_dirs: set[Path]) -> list[str]:
    ps = _POPEN(["ps", "axww", "-o", "pid=,args="], stdout=subprocess.PIPE, text=True)
    out = ps.communicate()[0]
    marks = [f"burner.host {d} " for d in run_dirs]
    return [line.strip() for line in out.splitlines() if any(m in line + " " for m in marks)]


@pytest.fixture(autouse=True)
def leak_guard(monkeypatch) -> Iterator[None]:
    """Record the sessions a test starts, and report what they leave behind.

    It looks only at the socket dirs and run dirs of those sessions, because
    other programs on the machine create their own entries in /tmp.
    """
    start = Host.start

    @functools.wraps(start)
    def recording_start(self: Host, name: str, *args, **kwargs):
        try:
            return start(self, name, *args, **kwargs)
        finally:
            meta = None
            with contextlib.suppress(Exception):
                meta = paths.read_json(self.run_dir / f"{name}.json")
            socket = meta.get("socket") if meta else None
            _started.append((self.run_dir, name, socket if isinstance(socket, str) else None))

    _started.clear()
    monkeypatch.setattr(Host, "start", recording_start)
    monkeypatch.setattr(client_module, "_FILE_CHECK_SECONDS", TEST_FILE_CHECK_SECONDS)
    yield
    problems = []
    run_dirs = {d for d, _, _ in _started}
    deadline = time.monotonic() + HOST_EXIT_WAIT
    hosts: list[str] = []
    # A test that started no session needs no process scan.
    while run_dirs and (hosts := _host_pids(run_dirs)) and time.monotonic() < deadline:
        time.sleep(0.1)
    problems += [f"host still running: {line}" for line in hosts]
    for _, _, socket in _started:
        if socket and socket.startswith("/tmp/burner-") and Path(socket).parent.exists():
            problems.append(f"socket dir {Path(socket).parent} remains")
    _started.clear()
    _report(problems)


def _cleanup(run_dir: Path) -> None:
    """Stop what the test left running, then report files an ended session must not leave."""
    host = Host(run_dir, timeout=3)
    for lock in run_dir.glob("*.lock"):
        name = lock.name[: -len(".lock")]
        if host.alive(name):
            host.stop(name, grace=1.0)
    _report(_leftover_files(run_dir))


@pytest.fixture
def run_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="sh-", dir="/tmp"))
    yield path
    try:
        _cleanup(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def long_run_dir(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / ("x" * 60) / ("y" * 60)
    yield path
    if path.exists():
        _cleanup(path)


@pytest.fixture
def host(run_dir: Path) -> Host:
    return Host(run_dir, timeout=5)
