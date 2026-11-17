from __future__ import annotations

import json
import selectors
import socket
import subprocess
import time
from pathlib import Path

import pytest
from conftest import ECHO, PY, STUBBORN, wait_for

from burner import Host, paths, protocol
from burner import host as host_module


def connect(run_dir: Path, name: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(json.loads((run_dir / f"{name}.json").read_text())["socket"])
    return sock


def ask_raw(run_dir: Path, name: str, payload: bytes) -> dict | None:
    """Send one raw J frame and return the reply, or None when the host closed."""
    with connect(run_dir, name) as sock:
        sock.sendall(protocol.frame(protocol.JSON, payload))
        try:
            return protocol.read_frame(sock, bytearray())[1]
        except (EOFError, OSError):
            return None


def start_echo(host: Host, run_dir: Path, name: str = "tui") -> None:
    host.start(name, [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen(name))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"op": "attach", "cols": 1e999, "rows": 5}',
        b'{"op": "stop", "grace": ' + b"9" * 400 + b"}",
        b'{"op": "stop", "grace": -1}',
        b'{"op": "stop", "grace": NaN}',
        b'{"op": "stop", "grace": "5"}',
        b'{"op": "screen", "lines": 1e999}',
        b"[" * 200_000,
    ],
)
def test_no_request_ends_a_session(host: Host, run_dir: Path, payload: bytes) -> None:
    start_echo(host, run_dir)
    reply = ask_raw(run_dir, "tui", payload)
    if reply is not None:
        assert reply["ok"] is False, reply
    assert host.alive("tui")
    assert "ready" in host.screen("tui")


def test_deep_nesting_is_a_protocol_error() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(bytearray(protocol.frame(protocol.JSON, b"[" * 200_000)))


def test_check_size() -> None:
    assert paths.check_size(1000, 100) == (1000, 100)
    assert paths.check_size(1, 1) == (1, 1)
    for cols, rows in ((0, 5), (5, 0), (1001, 1), (1, 1001), (1000, 101), (400, 400)):
        with pytest.raises(ValueError):
            paths.check_size(cols, rows)
    for cols, rows in ((80.0, 24), (True, 24), ("80", 24)):
        with pytest.raises(TypeError):
            paths.check_size(cols, rows)


def run_host(run_dir: Path, name: str, request: dict) -> dict:
    proc = subprocess.run(
        [PY, "-m", "burner.host", str(run_dir), name],
        input=protocol.frame(protocol.JSON, json.dumps(request).encode()),
        capture_output=True, timeout=10,
    )
    return json.loads(proc.stdout.decode().splitlines()[0])


@pytest.mark.parametrize("size", [(5000, 24), (80, 0), (400, 400)])
def test_host_refuses_a_bad_size_before_it_creates_files(run_dir: Path, size) -> None:
    request = {"argv": ["true"], "cwd": "/", "env": {}, "terminal": True,
               "cols": size[0], "rows": size[1], "log": None}
    reply = run_host(run_dir, "big", request)
    assert reply["ok"] is False
    assert list(run_dir.iterdir()) == []


def test_attach_and_resize_clamp_a_large_terminal(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir)
    with connect(run_dir, "tui") as sock:
        sock.sendall(protocol.message({"op": "attach", "cols": 2000, "rows": 2000}))
        assert protocol.read_frame(sock, bytearray())[1]["ok"]
        info = Host(run_dir)._call("tui", {"op": "info"})
        assert info["cols"] == 1000 and info["rows"] == 100
        sock.sendall(protocol.message({"op": "resize", "cols": 300, "rows": 900}))
        wait_for(lambda: Host(run_dir)._call("tui", {"op": "info"})["rows"] == 333)
        sock.sendall(protocol.frame(protocol.JSON, b'{"op": "resize", "cols": 1e999, "rows": 5}'))
        buf = bytearray()
        while (got := protocol.read_frame(sock, buf))[0] != protocol.JSON:
            pass
        assert got[1]["ok"] is False
    assert "ready" in host.screen("tui")


def test_a_second_stop_with_a_shorter_grace_moves_the_deadlines(
    host: Host, run_dir: Path
) -> None:
    host.start("stub", [PY, "-c", STUBBORN], cwd=run_dir)
    wait_for(lambda: "stubborn" in host.tail("stub"))
    with connect(run_dir, "stub") as sock:
        sock.sendall(protocol.message({"op": "stop", "grace": 600}))
        assert protocol.read_frame(sock, bytearray())[1]["ok"]
    started = time.monotonic()
    host.stop("stub", grace=0.5)
    assert host.gone("stub")
    assert time.monotonic() - started < 2.0


def _bare_host(run_dir: Path) -> host_module.SessionHost:
    return host_module.SessionHost(run_dir, "bare", {"terminal": True}, 0o022)


def _connect(h: host_module.SessionHost) -> tuple[host_module.Client, socket.socket]:
    ours, peer = socket.socketpair()
    ours.setblocking(False)
    c = host_module.Client(ours)
    h.clients[c] = lambda m: h._on_client(c, m)
    h.sel.register(ours, selectors.EVENT_READ, h.clients[c])
    return c, peer


def test_an_unexpected_error_in_a_request_gets_an_error_reply(run_dir: Path) -> None:
    h = _bare_host(run_dir)

    def broken(c, req):
        raise ZeroDivisionError("boom")

    h._op_info = broken
    c, peer = _connect(h)
    peer.sendall(protocol.message({"op": "info"}))
    h._on_client(c, selectors.EVENT_READ)
    peer.settimeout(2)
    reply = protocol.read_frame(peer, bytearray())[1]
    assert reply["ok"] is False and "boom" in reply["error"]
    assert c in h.clients
    peer.close()


def test_an_unexpected_error_in_a_callback_drops_only_that_client(run_dir: Path, capfd) -> None:
    h = _bare_host(run_dir)
    c, peer = _connect(h)
    other, other_peer = _connect(h)

    def broken(c, req):
        raise ZeroDivisionError("boom")

    h._request = broken
    peer.sendall(protocol.message({"op": "info"}))
    h._on_client(c, selectors.EVENT_READ)
    assert c not in h.clients and other in h.clients
    err = capfd.readouterr().err
    assert err.count("\n") == 1 and "boom" in err
    peer.close()
    other_peer.close()
