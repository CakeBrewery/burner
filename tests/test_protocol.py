from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from conftest import ECHO, PY, wait_for

from burner import Host, protocol


def connect(run_dir: Path, name: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(json.loads((run_dir / f"{name}.json").read_text())["socket"])
    return sock


def ask(sock: socket.socket, request: dict) -> dict:
    sock.sendall(protocol.message(request))
    kind, reply = protocol.read_frame(sock, bytearray())
    assert kind == protocol.JSON
    return reply


def start(host: Host, run_dir: Path) -> None:
    host.start("tui", [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))


def test_unknown_op_keeps_connection(host: Host, run_dir: Path) -> None:
    start(host, run_dir)
    with connect(run_dir, "tui") as sock:
        reply = ask(sock, {"op": "teleport"})
        assert reply["ok"] is False and "unknown op" in reply["error"]
        assert reply["v"] == protocol.VERSION
        reply = ask(sock, {"op": "info", "future_field": 1})
        assert reply["ok"] and reply["terminal"] and reply["cols"] == 80


def test_bad_requests_get_errors(host: Host, run_dir: Path) -> None:
    start(host, run_dir)
    with connect(run_dir, "tui") as sock:
        assert not ask(sock, {"op": "write", "data": "not base64!"})["ok"]
        assert not ask(sock, {"op": "screen", "lines": "x"})["ok"]
        assert not ask(sock, {"op": "attach", "cols": 0, "rows": 5})["ok"]
        assert ask(sock, {"op": "screen"})["ok"]


def test_garbage_and_broken_clients(host: Host, run_dir: Path) -> None:
    start(host, run_dir)
    with connect(run_dir, "tui") as sock:
        sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
        kind, reply = protocol.read_frame(sock, bytearray())
        assert reply["ok"] is False
        assert sock.recv(10) == b""
    with connect(run_dir, "tui") as sock:
        sock.sendall(protocol.frame(protocol.JSON, b"[1, 2]"))
        assert protocol.read_frame(sock, bytearray())[1]["ok"] is False
    with connect(run_dir, "tui") as sock:
        sock.sendall(b"J\x00\x00\x10\x00{")
    with connect(run_dir, "tui") as sock:
        sock.sendall(b"J\xff\xff\xff\xff")
        assert "too large" in protocol.read_frame(sock, bytearray())[1]["error"]
    assert "ready" in host.screen("tui")


def test_client_that_stops_reading_is_dropped(host: Host, run_dir: Path) -> None:
    script = "import sys\nwhile True: sys.stdout.write('y' * 4000 + '\\n')"
    host.start("flood", [PY, "-c", script], cwd=run_dir, terminal=True)
    wait_for(lambda: "yyyy" in host.screen("flood"))
    sock = connect(run_dir, "flood")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        assert ask(sock, {"op": "attach", "cols": 80, "rows": 24})["ok"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            started = time.monotonic()
            assert "yyyy" in host.screen("flood")
            assert time.monotonic() - started < 2
            info = Host(run_dir)._call("flood", {"op": "info"})
            if not info["attached"]:
                break
            time.sleep(0.2)
        assert not info["attached"]
    finally:
        sock.close()


def test_frame_parse() -> None:
    buf = bytearray(protocol.message({"op": "x"}) + protocol.frame(protocol.DATA, b"ab"))
    kind, value = protocol.parse(buf)
    assert kind == protocol.JSON and value == {"v": protocol.VERSION, "op": "x"}
    assert protocol.parse(buf) == (protocol.DATA, b"ab")
    assert protocol.parse(buf) is None
    assert protocol.parse(bytearray(b"D\x00\x00\x00\x05ab")) is None
