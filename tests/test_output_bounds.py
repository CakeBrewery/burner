from __future__ import annotations

import json
import os
import random
import re
import selectors
import socket
import subprocess
import threading
import time
import tty
from pathlib import Path

import pytest
from conftest import PY, wait_for

from burner import Host, protocol
from burner import host as host_module
from burner.screen import _MAX_CELL as MAX_CELL
from burner.screen import Screen

FACTOR = float(os.environ.get("BURNER_BENCH_FACTOR", "1"))
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def fill(prefix: bytes, unit: bytes) -> bytes:
    return (prefix + unit * 4096)[:4096]


HOSTILE = {
    "rep": fill(b"x", b"\x1b[65535b"),
    "wide rep": fill("中".encode(), b"\x1b[65535b"),
    "insert rep": fill(b"\x1b[4hx", b"\x1b[65535b"),
    "skin tone rep": fill("\U0001f3fb".encode(), b"\x1b[65535b"),
    "erase display": fill(b"", b"\x1b[2J"),
    "reset": fill(b"", b"\x1bc"),
    "alignment": fill(b"", b"\x1b#8"),
    "combining": fill(b"a", "́".encode()),
}


@pytest.mark.parametrize("size", [(80, 24), (250, 70), (250, 400)])
@pytest.mark.parametrize("name", list(HOSTILE))
def test_a_hostile_read_is_cheap(name: str, size: tuple[int, int]) -> None:
    budget = (1.0 if size == (250, 400) else 0.1) * FACTOR
    screen = Screen(*size)
    started = time.perf_counter()
    screen.feed(HOSTILE[name])
    assert time.perf_counter() - started < budget


def tokens(rng: random.Random) -> str:
    choices = [
        lambda: rng.choice("abc xyz"),
        lambda: rng.choice("中文👍é"),
        lambda: rng.choice("́‍️\U0001f3fb"),
        lambda: rng.choice("\r\n\b\t\x0b\x07\x00\x7f"),
        lambda: chr(rng.randrange(0x80, 0xA0)),
        lambda: "\x1b[" + ";".join(str(rng.randrange(0, 70000)) for _ in range(rng.randrange(3)))
        + rng.choice("ABCDEFGHJKLMPSTXZ@`abdefghlnrsu"),
        lambda: "\x1b[?" + str(rng.choice([1, 6, 7, 25, 47, 1049, 2004])) + rng.choice("hl"),
        lambda: "\x1b" + rng.choice("78DEMHc=>"),
        lambda: "\x1b#8",
        lambda: "\x1b[4" + rng.choice("hl"),
        lambda: "\x1b]0;title" + rng.choice(["\x07", "\x1b\\", ""]),
        lambda: "\x1b[" + str(rng.randrange(1, 9)) + ";" + str(rng.randrange(1, 9)) + "r",
    ]
    return "".join(rng.choice(choices)() for _ in range(rng.randrange(1, 40)))


def assert_sound(screen: Screen) -> None:
    assert len(screen._grid) == screen.rows
    assert all(len(row) == screen.cols for row in screen._grid)
    assert 0 <= screen._y < screen.rows and 0 <= screen._x <= screen.cols
    assert not any(CONTROL.search(line) for line in screen.lines())


def test_fuzz_keeps_the_screen_sound() -> None:
    rng = random.Random(20261117)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        screen = Screen(rng.randrange(1, 30), rng.randrange(1, 12))
        for _ in range(30):
            data = tokens(rng).encode()
            if rng.random() < 0.1:
                data += bytes([rng.randrange(0x80, 0x100)])
            screen.feed(data)
            if rng.random() < 0.05:
                screen.resize(rng.randrange(1, 30), rng.randrange(1, 12))
            assert_sound(screen)


def test_repeat_equals_printing_one_character_at_a_time() -> None:
    rng = random.Random(7)
    for _ in range(3000):
        cols, rows = rng.randrange(1, 13), rng.randrange(1, 7)
        prefix = tokens(rng) + rng.choice(["x", "中", "👍", "\U0001f3fb", "é"])
        fast, slow = Screen(cols, rows), Screen(cols, rows)
        fast.feed(prefix.encode())
        slow.feed(prefix.encode())
        n = rng.randrange(1, cols * rows * 2 + 2)
        fast.feed(b"\x1b[%db" % n)
        for _ in range(min(n, cols * rows)):
            slow._print(slow._last)
        state = (fast._grid, fast._x, fast._y, fast._last)
        assert state == (slow._grid, slow._x, slow._y, slow._last), (cols, rows, prefix, n)


def test_message_keeps_text_as_utf8() -> None:
    assert "é".encode() in protocol.message({"text": "é"})


def test_replies_may_be_larger_than_requests() -> None:
    big = protocol.message({"text": "x" * (4 << 20)})
    a, b = socket.socketpair()
    with a, b:
        b.settimeout(5)
        t = threading.Thread(target=a.sendall, args=(big,))
        t.start()
        assert len(protocol.read_frame(b, bytearray())[1]["text"]) == 4 << 20
        t.join()


def _bare_pty_host(run_dir: Path) -> tuple[host_module.SessionHost, int]:
    h = host_module.SessionHost(run_dir, "bare", {"terminal": True}, 0o022)
    h.screen = Screen(80, 24)
    h.master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(h.master, False)
    h.pty_open = True
    h.sel.register(h.master, selectors.EVENT_READ, h._on_pty)
    return h, slave


def test_query_replies_stop_while_the_child_does_not_read(run_dir: Path) -> None:
    h, slave = _bare_pty_host(run_dir)
    try:
        h.pty_out += b"x" * (host_module.QUERY_REPLY_LIMIT + 1)
        os.write(slave, b"\x1b[6n")
        wait_for(lambda: h._read_pty())
        assert len(h.pty_out) == host_module.QUERY_REPLY_LIMIT + 1
    finally:
        os.close(slave)
        os.close(h.master)


def test_a_screen_error_resets_the_screen(run_dir: Path, capfd) -> None:
    h, slave = _bare_pty_host(run_dir)
    try:
        old = h.screen

        def broken(data: bytes) -> None:
            raise ZeroDivisionError("boom")

        old.feed = broken
        os.write(slave, b"hello")
        wait_for(lambda: h._read_pty())
        assert h.screen is not old and h.screen.text() == ""
        err = capfd.readouterr().err
        assert err.count("\n") == 1 and "boom" in err
    finally:
        os.close(slave)
        os.close(h.master)


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or 0) / 1024


FLOOD = r"""
import os, tty
tty.setraw(0)
while True:
    os.write(1, b"\x1b[6n" * 1024)
"""


def test_a_query_flood_keeps_the_host_small(host: Host, run_dir: Path) -> None:
    host.start("flood", [PY, "-c", FLOOD], cwd=run_dir, terminal=True)
    host_pid = json.loads((run_dir / "flood.json").read_text())["pid"]
    peak = 0.0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        peak = max(peak, rss_mb(host_pid))
        time.sleep(0.25)
    assert host.alive("flood")
    assert peak < 32


MARKS = r"""
import os, time
cell = "a" + "́" * 40
row = cell * 250
for y in range(400):
    os.write(1, ("\x1b[%d;1H" % (y + 1) + row).encode())
time.sleep(60)
"""


def test_screen_of_a_full_large_terminal(host: Host, run_dir: Path) -> None:
    host.start("marks", [PY, "-c", MARKS], cwd=run_dir, terminal=True, cols=250, rows=400)
    cell = "a" + "\u0301" * (MAX_CELL - 1)
    full = "\n".join([cell * 250] * 400)
    # Rows fill in as the child writes, so wait for the whole screen.
    assert wait_for(lambda: host.screen("marks") == full, timeout=20)


def test_a_long_emoji_sequence_fits_one_cell() -> None:
    # Man, woman, girl, and boy, each with a skin tone, joined: 11 code points.
    people = ("\U0001f468", "\U0001f469", "\U0001f467", "\U0001f466")
    family = "\u200d".join(p + "\U0001f3fb" for p in people)
    screen = Screen(10, 2)
    screen.feed(family.encode() + b"x")
    assert screen.lines()[0] == family + "x"


@pytest.mark.parametrize("rows", [[], [""], ["plain"], ['a "quoted" \\ line', "é中́👍", ""],
                                  ["x" * 300] * 50])
def test_a_text_reply_built_in_place_matches_message(rows: list[str]) -> None:
    buf = bytearray(b"earlier")
    protocol.append_text_reply(buf, rows)
    assert bytes(buf) == b"earlier" + protocol.message({"ok": True, "text": "\n".join(rows)})


def test_text_rows_match_text() -> None:
    screen = Screen(10, 5)
    screen.feed(b"one\r\ntwo\r\n\r\nfour\r\n")
    for last in (None, 0, 1, 3, 9):
        assert "\n".join(screen.text_rows(last)) == screen.text(last)
