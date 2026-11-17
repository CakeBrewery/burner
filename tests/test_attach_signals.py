from __future__ import annotations

import os
import signal
import termios
from pathlib import Path

import pytest
from conftest import ECHO, PY, wait_for
from test_attach import Terminal

from burner import Host
from burner.attach import LEAVE


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_attach_restores_the_terminal_on_a_signal(host: Host, run_dir: Path, sig: int) -> None:
    host.start("tui", [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))
    term = Terminal(run_dir, "tui")
    try:
        term.read_until(b"size:80x24")
        assert not termios.tcgetattr(term.master)[3] & termios.ICANON
        os.kill(term.proc.pid, sig)
        assert term.wait() == -sig
        note = b"[detached: %s]\r\n" % signal.Signals(sig).name.encode()
        assert term.output.endswith(LEAVE + note)
        assert termios.tcgetattr(term.master)[3] & termios.ICANON
        assert host.alive("tui")
    finally:
        term.close()
