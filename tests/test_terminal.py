from __future__ import annotations

import threading
from pathlib import Path

import pytest
from conftest import ECHO, PY, wait_for

from burner import Host, HostError, Key, NotRunningError
from burner.host import answer_queries
from burner.keys import encode


def start_echo(host: Host, run_dir: Path, name: str = "tui", prefix: str = "") -> None:
    argv = [PY, "-c", ECHO] + ([prefix] if prefix else [])
    host.start(name, argv, cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen(name))


def test_screen_shows_output(host: Host, run_dir: Path) -> None:
    script = "import time; print('line one'); print('line two'); time.sleep(60)"
    host.start("out", [PY, "-c", script], cwd=run_dir, terminal=True)
    wait_for(lambda: "line two" in host.screen("out"))
    assert host.screen("out") == "line one\nline two"
    assert host.screen("out", lines=1) == "line two"


def test_terminal_size(host: Host, run_dir: Path) -> None:
    script = "import os, time; print(os.get_terminal_size()); time.sleep(60)"
    host.start("size", [PY, "-c", script], cwd=run_dir, terminal=True, cols=100, rows=30)
    wait_for(lambda: "columns=100, lines=30" in host.screen("size"))


def test_send_and_press(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir)
    host.send("tui", "hi")
    wait_for(lambda: "got:6869\ngot:0d" in host.screen("tui"))
    host.press("tui", Key.UP, "C-c", "Enter")
    wait_for(lambda: "got:1b5b41030d" in host.screen("tui"))
    host.write("tui", "é".encode())
    wait_for(lambda: "got:c3a9" in host.screen("tui"))


def test_arrow_keys_follow_application_mode(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir, prefix="\x1b[?1h")
    host.press("tui", "Up", "Home")
    wait_for(lambda: "got:1b4f411b4f48" in host.screen("tui"))


def test_send_rejects_newlines(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir)
    with pytest.raises(ValueError):
        host.send("tui", "a\nb")


def test_unknown_key(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir)
    with pytest.raises(ValueError, match="unknown key"):
        host.press("tui", "Up", "Hyper")


def test_input_to_gone_or_log_session(host: Host, run_dir: Path) -> None:
    with pytest.raises(HostError, match="is not running"):
        host.press("missing", "Enter")
    with pytest.raises(NotRunningError):
        host.send("missing", "x")
    with pytest.raises(NotRunningError):
        host.screen("missing")
    host.start("log", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    for call in (lambda: host.write("log", b"x"), lambda: host.screen("log")):
        with pytest.raises(HostError, match="log session"):
            call()


QUERY = r"""
import os, sys, tty
tty.setraw(0)
os.write(1, b"abc\x1b[6n")
first = os.read(0, 100)
os.write(1, b"\x1b[5n\x1b[c")
second = b""
while second.count(b"\x1b") < 2:
    second += os.read(0, 100)
os.write(1, b"\r\n" + first.hex().encode() + b" " + second.hex().encode() + b"\r\n")
import time; time.sleep(60)
"""


def test_terminal_queries_answered(host: Host, run_dir: Path) -> None:
    host.start("q", [PY, "-c", QUERY], cwd=run_dir, terminal=True)
    expected = (b"\x1b[1;4R").hex() + " " + (b"\x1b[0n\x1b[?62;22c").hex()
    wait_for(lambda: expected in host.screen("q"))


def test_answer_queries() -> None:
    assert answer_queries(b"x\x1b[6n", (4, 9)) == b"\x1b[5;10R"
    assert answer_queries(b"\x1b[5n\x1b[0c", (0, 0)) == b"\x1b[0n\x1b[?62;22c"
    assert answer_queries(b"\x1b[?6n\x1b[>c\x1b[2J", (0, 0)) == b""


def test_fast_output_without_client(host: Host, run_dir: Path) -> None:
    script = "import sys; sys.stdout.write('x' * 1_000_000 + '\\nDONE\\n'); sys.stdout.flush()"
    host.start("flood", [PY, "-c", script], cwd=run_dir, terminal=True)
    wait_for(lambda: host.gone("flood"), timeout=15)
    assert host.exit_status("flood").code == 0


def test_many_concurrent_clients(host: Host, run_dir: Path) -> None:
    start_echo(host, run_dir)
    results: list[str] = []
    errors: list[Exception] = []

    def read() -> None:
        try:
            results.append(host.screen("tui"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 40 and all("ready" in r for r in results)


def test_key_encoding() -> None:
    assert encode("Enter") == b"\r"
    assert encode(Key.DOWN) == b"\x1b[B"
    assert encode(Key.DOWN, True) == b"\x1bOB"
    assert encode("C-a") == b"\x01" and encode("C-z") == b"\x1a"
    assert encode("PageDown") == b"\x1b[6~"
    assert encode(Key.BACK_TAB) == b"\x1b[Z"
    assert encode(Key.INSERT) == b"\x1b[2~"
    assert encode(Key.F1) == b"\x1bOP" and encode("F4") == b"\x1bOS"
    assert encode("F5") == b"\x1b[15~" and encode(Key.F12) == b"\x1b[24~"
    assert len({encode(f"F{n}") for n in range(1, 13)}) == 12
    for bad in ("C-1", "F13", "F0", "Hyper", ""):
        with pytest.raises(ValueError, match="unknown key"):
            encode(bad)


def test_key_names_ignore_case() -> None:
    assert encode("up") == encode("UP") == encode(Key.UP)
    assert encode("pagedown") == encode("PageDown")
    assert encode("c-A") == encode("C-a") == b"\x01"
    assert encode("f10") == encode("F10")
    assert encode("backtab") == b"\x1b[Z"


def test_unknown_key_message() -> None:
    with pytest.raises(ValueError, match=r"^unknown key 'Hyper' \(see burner press --help\)$"):
        encode("Hyper")
