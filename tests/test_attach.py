from __future__ import annotations

import contextlib
import fcntl
import os
import select
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest
from conftest import ECHO, PY, wait_for

from burner import Host, HostError


class Terminal:
    """A PTY pair with `burner attach` running on the slave side."""

    def __init__(self, run_dir: Path, name: str, cols: int = 80, rows: int = 24) -> None:
        self.master, slave = os.openpty()
        self.resize(cols, rows, fd=slave)

        def controlling_tty() -> None:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(
            [PY, "-m", "burner.cli", "--run-dir", str(run_dir), "attach", name],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            preexec_fn=controlling_tty,
        )
        os.close(slave)
        self.output = b""

    def resize(self, cols: int, rows: int, fd: int | None = None) -> None:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master if fd is None else fd, termios.TIOCSWINSZ, size)

    def read_until(self, text: bytes, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        while text not in self.output:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{text!r} not seen; got {self.output[-500:]!r}")
            if select.select([self.master], [], [], left)[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    raise AssertionError(f"{text!r} not seen before EOF; got {self.output!r}")
                self.output += chunk
        return self.output

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def wait(self) -> int:
        """Wait for attach to exit, reading its output so it never blocks on a full PTY."""
        deadline = time.monotonic() + 5
        while self.proc.poll() is None and time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.05)[0]:
                with contextlib.suppress(OSError):
                    self.output += os.read(self.master, 65536)
        code = self.proc.wait(1)
        try:
            while select.select([self.master], [], [], 0.05)[0]:
                chunk = os.read(self.master, 65536)
                if not chunk:
                    break
                self.output += chunk
        except OSError:
            pass
        return code

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)


@pytest.fixture
def terminals():
    made: list[Terminal] = []

    def make(*args, **kwargs) -> Terminal:
        made.append(Terminal(*args, **kwargs))
        return made[-1]

    yield make
    for t in made:
        t.close()


def start_echo(host: Host, run_dir: Path) -> None:
    host.start("tui", [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))


def test_attach_input_resize_detach(host: Host, run_dir: Path, terminals) -> None:
    start_echo(host, run_dir)
    term = terminals(run_dir, "tui", cols=100, rows=30)
    term.read_until(b"size:100x30")
    assert b"ready" in term.output
    term.send(b"hi")
    term.read_until(b"got:6869")
    term.resize(120, 40)
    term.read_until(b"size:120x40")
    term.send(b"\x1c")
    assert term.wait() == 0
    assert b"[detached]" in term.output
    assert host.alive("tui")
    wait_for(lambda: host.screen("tui").endswith("size:80x24"))


def test_attach_at_same_size_still_repaints(host: Host, run_dir: Path, terminals) -> None:
    start_echo(host, run_dir)
    term = terminals(run_dir, "tui")
    term.read_until(b"size:80x24")
    term.send(b"x\x1c")
    assert term.wait() == 0
    wait_for(lambda: host.screen("tui").count("size:80x24") == 2)
    assert "got:78" in host.screen("tui")


def test_new_attach_detaches_old(host: Host, run_dir: Path, terminals) -> None:
    start_echo(host, run_dir)
    first = terminals(run_dir, "tui")
    first.read_until(b"size:80x24")
    second = terminals(run_dir, "tui", cols=90, rows=20)
    assert first.wait() == 0
    assert b"another client attached" in first.output
    second.read_until(b"size:90x20")
    second.send(b"\x1c")
    assert second.wait() == 0


def test_attach_returns_exit_code_when_session_ends(host: Host, run_dir: Path, terminals):
    start_echo(host, run_dir)
    term = terminals(run_dir, "tui")
    term.read_until(b"size:80x24")
    term.send(b"q")
    assert term.wait() == 7
    assert b"[session ended]" in term.output
    wait_for(lambda: host.gone("tui"))


def test_attach_sets_the_session_input_modes_and_detach_resets_them(
    host: Host, run_dir: Path, terminals
) -> None:
    modes = "\x1b[?1h\x1b[?2004h\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[?1004h"
    host.start("tui", [PY, "-c", ECHO, modes], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))
    term = terminals(run_dir, "tui")
    term.read_until(b"size:80x24")
    repaint = term.output.index(b"\x1b[H\x1b[2J")
    for mode in (1, 1000, 1002, 1004, 1006, 2004):
        assert term.output.index(b"\x1b[?%dh" % mode) < repaint
    assert b"\x1b[?1003h" not in term.output
    term.send(b"\x1c")
    assert term.wait() == 0
    tail = term.output[term.output.rindex(b"\x1b[0m\x1b[?25h") :]
    for mode in (1, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 2004):
        assert b"\x1b[?%dl" % mode in tail


ASK = r"""
import os, select, sys, tty
tty.setraw(0)
os.write(1, b"ready\r\n")
while True:
    os.read(0, 100)
    os.write(1, b"\x1b[6n")
    reply = os.read(0, 100) if select.select([0], [], [], 1.0)[0] else b"none"
    os.write(1, b"reply:" + reply.hex().encode() + b"\r\n")
"""


def test_host_does_not_answer_queries_while_attached(host: Host, run_dir: Path, terminals):
    host.start("ask", [PY, "-c", ASK], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("ask"))
    term = terminals(run_dir, "ask")
    term.read_until(b"ready")
    term.send(b"x")
    term.read_until(b"reply:" + b"none".hex().encode())
    term.send(b"\x1c")
    assert term.wait() == 0
    host.write("ask", b"x")
    wait_for(lambda: "reply:1b5b" in host.screen("ask"))


def test_attach_needs_a_terminal(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir)
    result = subprocess.run(
        [PY, "-m", "burner.cli", "--run-dir", str(run_dir), "attach", "tui"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 125
    assert "needs a terminal" in result.stderr
    assert host.alive("tui")


def test_attach_refuses_gone_and_log_sessions(host: Host, run_dir: Path) -> None:
    with pytest.raises(HostError, match="is not running"):
        host.attach("missing")
    host.start("log", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    with pytest.raises(HostError, match="log session"):
        host.attach("log")


def test_frames_that_arrive_with_the_attach_reply_are_shown(capfd, monkeypatch) -> None:
    from burner import attach, protocol

    class NoWait:
        def register(self, *args: object) -> None:
            pass

        def select(self) -> list:
            raise AssertionError("waited for input while frames sat in the buffer")

    # The host sends its reply and the first screen back to back. When both
    # arrive in one read, the screen is already in the buffer before the loop.
    monkeypatch.setattr(attach.selectors, "DefaultSelector", NoWait)
    buf = bytearray(
        protocol.frame(protocol.DATA, b"first screen")
        + protocol.message({"event": "exit", "code": 3})
    )
    assert attach._pump(None, buf, -1) == (3, "session ended")
    assert "first screen" in capfd.readouterr().out
