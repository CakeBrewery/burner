from __future__ import annotations

import contextlib
import json
import os
import select
import selectors
import shutil
import signal
import socket
import subprocess
import threading
import time
import tty
from pathlib import Path

import pytest
from conftest import ECHO, PY, pid_alive, wait_for

from burner import Host, paths, protocol
from burner import host as host_module
from burner.host import ExitWatch


def test_probes_never_see_each_other_as_a_host(run_dir: Path) -> None:
    lock = run_dir / "free.lock"
    lock.touch()
    seen: list[bool | None] = []
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            seen.append(paths.lock_probe(lock))

    threads = [threading.Thread(target=probe) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(2000):
        seen.append(paths.lock_probe(lock))
    stop.set()
    for t in threads:
        t.join()
    assert set(seen) == {False}


def test_start_is_not_refused_while_others_probe(host: Host, run_dir: Path) -> None:
    (run_dir / "busy.lock").touch()
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            host.alive("busy")

    threads = [threading.Thread(target=probe) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        host.start("busy", ["sleep", "30"], cwd=run_dir)
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert host.alive("busy")


def test_exit_watch_sees_the_exit_and_leaves_the_child_unreaped() -> None:
    child = subprocess.Popen(["sleep", "0.2"], start_new_session=True)
    watch = ExitWatch(child.pid)
    try:
        assert not watch.check()
        wait_for(watch.check)
        # The zombie still holds its pid, so its group exists. macOS answers
        # EPERM for a group of zombies and ESRCH only for a group that is gone.
        with contextlib.suppress(PermissionError):
            os.killpg(child.pid, 0)
        assert child.returncode is None
    finally:
        watch.close()
        assert child.wait(5) == 0


def test_exit_watch_on_a_child_that_already_exited() -> None:
    child = subprocess.Popen(["true"])
    time.sleep(0.3)
    watch = ExitWatch(child.pid)
    try:
        assert watch.check()
    finally:
        watch.close()
        assert child.wait(5) == 0


GROUP = r"""
import os, subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(p.pid))
time.sleep(0.3)
sys.exit(3)
"""


@pytest.mark.parametrize("terminal", [False, True])
def test_child_exit_hangs_up_on_its_group(
    host: Host, run_dir: Path, terminal: bool
) -> None:
    pid_file = run_dir / "grandchild.pid"
    host.start("grp", [PY, "-c", GROUP, str(pid_file)], cwd=run_dir, terminal=terminal)
    wait_for(lambda: host.gone("grp"))
    assert host.exit_status("grp").code == 3
    wait_for(lambda: not pid_alive(int(pid_file.read_text())))


# The grandchild ignores the hangup, keeps the PTY or the log open, and keeps
# writing to it after the child exits.
LINGER = r"""
import subprocess, sys
code = '''
import signal, sys, time
signal.signal(signal.SIGHUP, signal.SIG_IGN)
while True:
    try:
        print("x" * 200, flush=True)
    except OSError:
        pass
    time.sleep(0.001)
'''
p = subprocess.Popen([sys.executable, "-c", code])
open(sys.argv[1], "w").write(str(p.pid))
sys.exit(4)
"""


@pytest.mark.parametrize("terminal", [False, True])
def test_host_exits_when_grandchild_holds_the_output_open(
    host: Host, run_dir: Path, terminal: bool
) -> None:
    pid_file = run_dir / "grandchild.pid"
    host.start("lin", [PY, "-c", LINGER, str(pid_file)], cwd=run_dir, terminal=terminal)
    try:
        wait_for(lambda: host.gone("lin"))
        assert host.exit_status("lin").code == 4
        host.start("lin", ["sleep", "30"], cwd=run_dir)
        assert host.alive("lin")
    finally:
        grandchild = int(wait_for(pid_file.read_text))
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild, signal.SIGKILL)


def _bare_host(run_dir: Path) -> host_module.SessionHost:
    return host_module.SessionHost(run_dir, "bare", {"terminal": True}, 0o022)


def _connect(h: host_module.SessionHost) -> tuple[host_module.Client, socket.socket]:
    ours, peer = socket.socketpair()
    ours.setblocking(False)
    c = host_module.Client(ours)
    h.clients[c] = lambda m: h._on_client(c, m)
    h.sel.register(ours, selectors.EVENT_READ, h.clients[c])
    return c, peer


def test_a_closing_client_does_not_buffer_more_input(run_dir: Path) -> None:
    h = _bare_host(run_dir)
    c, peer = _connect(h)
    c.closing = True
    c.outbuf += b"a reply the client has not read"
    for _ in range(20):
        peer.sendall(b"J" * 4096)
        h._on_client(c, selectors.EVENT_READ)
    assert not c.inbuf
    peer.close()


def test_attached_input_is_bounded(run_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(host_module, "PTY_INPUT_LIMIT", 64 * 1024)
    h = _bare_host(run_dir)
    h.master, slave = os.openpty()
    tty.setraw(slave)  # In canonical mode macOS drops input past a full line.
    os.set_blocking(h.master, False)
    h.pty_open = True
    h.sel.register(h.master, selectors.EVENT_READ, h._on_pty)
    c, peer = _connect(h)
    h.attached = c
    try:
        # Nothing reads the slave. Once the PTY stops taking input, the rest
        # waits in the host, and input past the limit is dropped.
        pending = bytearray(protocol.frame(protocol.DATA, b"k" * 1024) * 1024)
        peer.setblocking(False)
        while pending:
            with contextlib.suppress(BlockingIOError):
                del pending[: peer.send(pending)]
            while select.select([c.sock], [], [], 0)[0]:
                h._on_client(c, selectors.EVENT_READ)
        assert 0 < len(h.pty_out) <= 64 * 1024
    finally:
        peer.close()
        os.close(slave)
        os.close(h.master)


def test_host_restores_a_removed_socket_and_meta(host: Host, run_dir: Path) -> None:
    host.start("tui", [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))
    sock = Path(json.loads((run_dir / "tui.json").read_text())["socket"])
    sock.unlink()
    (run_dir / "tui.json").unlink()
    wait_for(lambda: sock.exists() and (run_dir / "tui.json").exists(), timeout=10)
    host.send("tui", "hi")
    wait_for(lambda: "got:6869" in host.screen("tui"))


def test_deleting_the_run_dir_ends_the_session(host: Host, run_dir: Path) -> None:
    host.start("old", ["sleep", "60"], cwd="/")
    child = host.sessions()[0].pid
    shutil.rmtree(run_dir)
    # A new host takes the name in a new run dir at the same path.
    fresh = Host(run_dir, timeout=5)
    fresh.start("old", [PY, "-c", ECHO], cwd="/", terminal=True)
    wait_for(lambda: not pid_alive(child), timeout=10)
    assert fresh.alive("old")
    assert (run_dir / "old.json").exists() and not (run_dir / "old.exit").exists()
    wait_for(lambda: "ready" in fresh.screen("old"))


def test_a_cut_off_query_is_carried_only_while_short() -> None:
    assert host_module._PARTIAL_QUERY.fullmatch(b"\x1b[12")
    assert not host_module._PARTIAL_QUERY.fullmatch(b"\x1b[" + b"1" * 4096)


CLOSE_PTY = r"""
import os, time
print("closing", flush=True)
time.sleep(0.3)
devnull = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(devnull, fd)
time.sleep(60)
"""


def test_child_that_closes_its_terminal_keeps_the_session(host: Host, run_dir: Path) -> None:
    host.start("quiet", [PY, "-c", CLOSE_PTY], cwd=run_dir, terminal=True)
    wait_for(lambda: "closing" in host.screen("quiet"))
    time.sleep(1.0)
    assert host.alive("quiet")
    assert "closing" in host.screen("quiet")
    host.stop("quiet", grace=1.0)
    assert host.gone("quiet")
    assert host.exit_status("quiet").signal is not None
