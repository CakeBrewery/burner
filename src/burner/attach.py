"""Connect the caller's terminal to a terminal session."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import socket
import termios
import tty
from typing import Any

from . import protocol
from .errors import HostError
from .screen import INPUT_MODES

DETACH_KEY = b"\x1c"
# Signals that end an attach. The terminal is restored before the process ends.
ENDING_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# Show the session on the alternate screen so the caller's screen returns on detach.
ENTER = b"\x1b[?1049h"
# Undo what the attach or the session may have turned on: colors, a hidden
# cursor, every input mode the host replays, and the alternate screen.
LEAVE = (
    b"\x1b[0m\x1b[?25h"
    + b"".join(b"\x1b[?%dl" % mode for mode in INPUT_MODES)
    + b"\x1b[?1049l"
)


def attach(sock_path: str, timeout: float) -> int | None:
    """Forward bytes both ways until detach or the session ends.

    Returns 0 after a detach, the child's exit code when the host reports
    the end, or None when the host went away without a report.
    """
    if not (os.isatty(0) and os.isatty(1)):
        raise HostError("attach needs a terminal on stdin and stdout")
    cols, rows = os.get_terminal_size(1)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    buf = bytearray()
    try:
        sock.connect(sock_path)
        sock.sendall(protocol.message({"op": "attach", "cols": cols, "rows": rows}))
        _, reply = protocol.read_frame(sock, buf)
    except (OSError, EOFError, protocol.ProtocolError) as exc:
        sock.close()
        raise HostError(f"cannot attach: {exc}") from None
    if not reply.get("ok"):
        sock.close()
        raise HostError(f"cannot attach: {reply.get('error')}")

    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    saved = termios.tcgetattr(0)
    old: dict[int, Any] = {}
    note, ended = "detached", None
    try:
        old[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, lambda *_: _poke(wake_w))
        for sig in ENDING_SIGNALS:
            if signal.getsignal(sig) is not signal.SIG_IGN:
                old[sig] = signal.signal(sig, _end)
        tty.setraw(0)
        _write_all(ENTER)
        code, note = _pump(sock, buf, wake_r)
    except _Ended as exc:
        code, ended = None, exc.signum
        note = f"detached: {signal.Signals(ended).name}"
    finally:
        # A second ending signal must not cut the restore short.
        for sig in ENDING_SIGNALS:
            if sig in old:
                signal.signal(sig, signal.SIG_IGN)
        # After a hangup the terminal may be gone, so each step may fail alone.
        with contextlib.suppress(OSError):
            _write_all(LEAVE)
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(0, termios.TCSANOW, saved)
        sock.close()
        os.close(wake_r)
        os.close(wake_w)
        with contextlib.suppress(OSError):
            os.write(2, f"[{note}]\n".encode())
        for sig, handler in old.items():
            signal.signal(sig, handler)
    if ended is not None:
        # End the way the signal would have, now that the terminal is restored.
        signal.raise_signal(ended)
        return 128 + ended
    return code


class _Ended(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _end(signum: int, frame: object) -> None:
    raise _Ended(signum)


def _pump(sock: socket.socket, buf: bytearray, wake_r: int) -> tuple[int | None, str]:
    sel = selectors.DefaultSelector()
    sel.register(0, selectors.EVENT_READ)
    sel.register(sock, selectors.EVENT_READ)
    sel.register(wake_r, selectors.EVENT_READ)
    # Frames that arrived with the attach reply are already in the buffer.
    done = _show(buf)
    while done is None:
        for key, _ in sel.select():
            if key.fd == wake_r:
                while _drain(wake_r):
                    pass
                cols, rows = os.get_terminal_size(1)
                sock.sendall(protocol.message({"op": "resize", "cols": cols, "rows": rows}))
            elif key.fd == 0:
                data = os.read(0, 65536)
                cut = data.find(DETACH_KEY)
                if cut >= 0 or not data:
                    if cut > 0:
                        sock.sendall(protocol.frame(protocol.DATA, data[:cut]))
                    sock.sendall(protocol.message({"op": "detach"}))
                    return 0, "detached"
                sock.sendall(protocol.frame(protocol.DATA, data))
            else:
                chunk = sock.recv(65536)
                if not chunk:
                    return None, "session ended"
                buf += chunk
                done = _show(buf)
                if done is not None:
                    break
    return done


def _show(buf: bytearray) -> tuple[int | None, str] | None:
    """Write the frames in buf to the terminal. Returns the result once the attach ends."""
    while (got := protocol.parse(buf, protocol.MAX_REPLY)) is not None:
        kind, value = got
        if kind == protocol.DATA:
            _write_all(value)
        elif value.get("event") == "exit":
            code = value.get("code")
            if code is None:
                code = 128 + int(value.get("signal", 0))
            return int(code), "session ended"
        elif value.get("event") == "detached":
            return 0, f"detached: {value.get('reason', '')}"
    return None


def _write_all(data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def _poke(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.write(fd, b"x")


def _drain(fd: int) -> bool:
    try:
        return bool(os.read(fd, 64))
    except BlockingIOError:
        return False
