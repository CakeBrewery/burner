"""The host process for one session.

``Host.start`` runs ``python -m burner.host RUN_DIR NAME`` and writes the
start request to its stdin. The process forks once so it is not the
starter's child, starts the session's child, reports ready on stdout, and
then serves clients on its socket until the child exits.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import math
import os
import re
import select
import selectors
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import traceback
from pathlib import Path
from typing import Any

from . import paths, protocol
from .errors import HostError

# A log moves to NAME.log.1 before it grows past this, so it takes at most twice this on disk.
LOG_LIMIT_BYTES = 5 * 1024 * 1024
READ_SIZE = 65536
# Short PTY reads keep the time one screen update takes short.
PTY_READ_SIZE = 4096
CLIENT_BUFFER_LIMIT = protocol.MAX_REPLY
# Query replies wait while the child has this much input it has not read.
QUERY_REPLY_LIMIT = 64 * 1024
PTY_INPUT_LIMIT = 4 << 20
DRAIN_LIMIT = 4 << 20
IDLE_CLIENT_SECONDS = 30.0
LOCK_WAIT = 0.5
# How often a live host checks that its files are still in place.
FILE_CHECK_SECONDS = 5.0
# How often the host checks whether the child's group is empty after the child exited.
LINGER_POLL = 0.1
DEFAULT_GRACE = 5.0
# kqueue reports the exit status with this flag. Python names it only on newer versions.
KQ_NOTE_EXITSTATUS = getattr(select, "KQ_NOTE_EXITSTATUS", 0x04000000)

_QUERY = re.compile(rb"\x1b\[([0-9]*)([nc])")
# The start of a query cut off at the end of a read. The digit limit keeps the
# carried tail short whatever the program writes.
_PARTIAL_QUERY = re.compile(rb"\x1b(\[[0-9]{0,8})?")


def answer_queries(data: bytes, cursor: tuple[int, int]) -> bytes:
    """Return what a terminal would reply to the queries in data.

    The cursor is the zero-based (row, col) after data was shown.
    """
    out = []
    for match in _QUERY.finditer(data):
        arg, final = match.groups()
        if final == b"n" and arg == b"6":
            out.append(b"\x1b[%d;%dR" % (cursor[0] + 1, cursor[1] + 1))
        elif final == b"n" and arg == b"5":
            out.append(b"\x1b[0n")
        elif final == b"c" and arg in (b"", b"0"):
            out.append(b"\x1b[?62;22c")
    return b"".join(out)


class ExitWatch:
    """Tells when a child has exited, and how, without reaping it.

    An unreaped child keeps its pid, so the pid still names its process group
    and no new process can take it.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exited = False
        # {"code": N} or {"signal": N}. None after the exit when only a reap can tell.
        self.status: dict[str, int] | None = None
        self.kq: Any = None
        if not hasattr(os, "waitid"):
            # macOS has no waitid, so watch for the exit with kqueue.
            self.kq = select.kqueue()
            event = select.kevent(
                pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD,
                select.KQ_NOTE_EXIT | KQ_NOTE_EXITSTATUS,
            )
            try:
                self.kq.control([event], 0)
            except ProcessLookupError:
                self.exited = True  # Our own unreaped child: it is a zombie already.

    def check(self) -> bool:
        if self.exited:
            return True
        if self.kq is not None:
            events = self.kq.control(None, 1, 0)
            if events:
                self.exited = True
                self.status = _status(events[0].data)
        else:
            flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
            info = os.waitid(os.P_PID, self.pid, flags)
            if info is not None:
                self.exited = True
                exited = info.si_code == os.CLD_EXITED
                self.status = {"code" if exited else "signal": info.si_status}
        return self.exited

    def close(self) -> None:
        if self.kq is not None:
            self.kq.close()


class StartError(Exception):
    """The session could not start. The message goes back to the starter."""


class Client:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.closing = False
        self.writing = False
        self.last = time.monotonic()


class SessionHost:
    def __init__(self, run_dir: Path, name: str, req: dict[str, Any], umask: int) -> None:
        self.run_dir = run_dir
        self.name = name
        self.req = req
        self.umask = umask
        self.terminal = bool(req.get("terminal"))
        self.cols, self.rows = paths.check_size(req.get("cols", 80), req.get("rows", 24))
        check = req.get("_check_seconds")  # Only tests send it, for a quicker file check.
        ok = type(check) in (int, float) and 0.05 <= check <= FILE_CHECK_SECONDS
        self.check_seconds = check if ok else FILE_CHECK_SECONDS
        self.pty_size = (self.cols, self.rows)
        self.log = Path(req.get("log") or run_dir / f"{name}.log")
        self.lock_path = run_dir / f"{name}.lock"
        self.meta_path = run_dir / f"{name}.json"
        self.exit_path = run_dir / f"{name}.exit"
        self.sel = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.sock_path: Path | None = None
        self.sock_id: tuple[int, int] | None = None
        # A dir of this host's own under /tmp for a socket path too long for the run dir.
        self.sock_dir: Path | None = None
        self.sock_dir_fd = -1
        self.sock_dir_id: tuple[int, int] | None = None
        self.child: subprocess.Popen[bytes] | None = None
        self.log_fd = -1
        self.log_created = False
        self.log_size = 0
        self.log_error = ""
        self.log_pipe = -1  # The read end of the child's output, for a log session.
        self.log_to_stderr = False
        self.error = ""
        self.name_lost = False
        self.check_error = ""
        self.next_check = 0.0
        self.clients: dict[Client, Any] = {}
        self.attached: Client | None = None
        self.master = -1
        self.pty_open = False
        self.pty_out = bytearray()
        self.query_tail = b""
        self.screen: Any = None
        self.timers: dict[int, float] = {}  # Signal to the time it is due.
        self.stopping = False
        self.term_requested = False
        # After the child exits, the host keeps it unreaped while the rest of
        # its group gets the stop ladder.
        self.lingering = False
        self.reported = False  # The exit record is written and the name is free.
        self.done = False

    # Setup

    def setup(self) -> None:
        paths.make_run_dir(self.run_dir)
        self.lock_fd = self._lock()
        try:
            self._start()
        except BaseException:
            self.undo_start()
            raise

    def _lock(self) -> int:
        # Clients probe the lock for a moment, so retry briefly before refusing.
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                return paths.lock_file(self.lock_path, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise StartError(f"session {self.name} is already running") from None
                time.sleep(0.02)

    def _start(self) -> None:
        old = paths.read_json(self.meta_path)
        if old and isinstance(old.get("socket"), str):
            paths.remove_socket(old["socket"], self.run_dir)
        paths.unlink(self.meta_path)
        if not self.terminal:
            self.log_fd = self._open_log()
        self._install_signals()
        self._listen()
        self._spawn()
        paths.unlink(self.exit_path)
        self.meta = {
            "name": self.name,
            "pid": os.getpid(),
            "child": self.child.pid,
            "pgid": self.child.pid,
            "terminal": self.terminal,
            "cols": self.cols,
            "rows": self.rows,
            "log": str(self.log),
            "protocol": protocol.VERSION,
            "started": time.time(),
            "socket": str(self.sock_path),
            "sid": os.getsid(0),
        }
        paths.write_private(self.meta_path, self.meta)

    def undo_start(self) -> None:
        """Undo a start that failed: end the child, and remove every file this start made."""
        if self.child is not None:
            self._signal_group(signal.SIGKILL)
            self.child.wait()
        if self.log_created:
            with contextlib.suppress(OSError):  # A start that failed keeps no empty log.
                if os.fstat(self.log_fd).st_size == 0:
                    paths.unlink(self.log)
        self._remove_files()

    def _remove_files(self) -> None:
        """Remove the session's files. The lock goes last, while this host still holds it."""
        if self.sock_path is not None and _file_id(self.sock_path) == self.sock_id:
            paths.unlink(self.sock_path)
        if self.sock_dir is not None:
            if _file_id(self.sock_dir) == self.sock_dir_id:
                with contextlib.suppress(OSError):
                    os.rmdir(self.sock_dir)
            if self.sock_dir_fd >= 0:
                os.close(self.sock_dir_fd)
                self.sock_dir_fd = -1
            self.sock_dir = None
        if not self.name_lost:
            paths.unlink(self.meta_path)
        if paths.same_file(self.lock_path, self.lock_fd):
            paths.unlink(self.lock_path)
        fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def _open_log(self) -> int:
        try:
            if os.lstat(self.log).st_size >= LOG_LIMIT_BYTES:
                os.replace(self.log, f"{self.log}.1")
        except FileNotFoundError:
            pass
        fd, self.log_created = _open_append(self.log)
        self.log_size = os.fstat(fd).st_size
        return fd

    def _on_log_pipe(self, mask: int) -> None:
        self._read_log_pipe()

    def _read_log_pipe(self) -> int:
        """Copy what the child wrote to the log. Returns the byte count, 0 when nothing came."""
        try:
            data = os.read(self.log_pipe, READ_SIZE)
        except BlockingIOError:
            return 0
        if not data:
            self.sel.unregister(self.log_pipe)
            os.close(self.log_pipe)
            self.log_pipe = -1
            return 0
        full = self.log_size > 0 and self.log_size + len(data) > LOG_LIMIT_BYTES
        if full and not self._rotate_log():
            return len(data)  # Dropped until the log can move.
        try:
            _write_all(self.log_fd, data)
        except OSError:
            # The rest is dropped, as on a full disk. Count what reached the file.
            with contextlib.suppress(OSError):
                self.log_size = os.fstat(self.log_fd).st_size
            return len(data)
        self.log_size += len(data)
        return len(data)

    def _rotate_log(self) -> bool:
        """Move the log to NAME.log.1 and open a new one, which also takes host errors."""
        try:
            with contextlib.suppress(FileNotFoundError):  # Moved by an earlier try.
                os.replace(self.log, f"{self.log}.1")
            fd, _ = _open_append(self.log)
        except (OSError, StartError) as exc:
            error = f"burner: cannot rotate the log: {exc}"
            if error != self.log_error:
                print(error, file=sys.stderr, flush=True)
            self.log_error = error
            return False
        if self.log_to_stderr:
            os.dup2(fd, 2)
        os.close(self.log_fd)
        self.log_fd, self.log_size, self.log_error = fd, 0, ""
        return True

    def _install_signals(self) -> None:
        self.wake_r, wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        wake_w.setblocking(False)
        self.wake_w = wake_w
        signal.set_wakeup_fd(wake_w.fileno(), warn_on_full_buffer=False)
        signal.signal(signal.SIGCHLD, lambda *_: None)
        signal.signal(signal.SIGTERM, self._on_sigterm)
        self.sel.register(self.wake_r, selectors.EVENT_READ, self._on_wake)

    def _socket_path(self) -> Path:
        path = self.run_dir / f"{self.name}.sock"
        if len(os.fsencode(path)) <= paths.MAX_SOCKET_PATH:
            return path
        if self.sock_dir is None or _file_id(self.sock_dir) != self.sock_dir_id:
            self._make_socket_dir()
        path = self.sock_dir / f"{self.name}.sock"
        if len(os.fsencode(path)) > paths.MAX_SOCKET_PATH:
            path = self.sock_dir / "session.sock"
        return path

    def _make_socket_dir(self) -> None:
        if self.sock_dir_fd >= 0:
            os.close(self.sock_dir_fd)
            self.sock_dir_fd = -1
        sock_dir = Path(tempfile.mkdtemp(prefix="burner-", dir="/tmp"))
        fd = -1
        try:
            fd = os.open(sock_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            # systemd-tmpfiles skips entries that hold a BSD lock.
            fcntl.flock(fd, fcntl.LOCK_SH)
        except BaseException:
            # The host keeps no record of this dir yet, so remove it here.
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(OSError):
                os.rmdir(sock_dir)
            raise
        self.sock_dir, self.sock_dir_fd = sock_dir, fd
        self.sock_dir_id = _file_id(sock_dir)

    def _listen(self) -> None:
        """Listen on the session's socket path, replacing an earlier listener."""
        path = self._socket_path()
        paths.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(path))
            os.chmod(path, 0o600)
            listener.listen(64)
            listener.setblocking(False)
            self.sock_id = _file_id(path)
        except BaseException:
            listener.close()
            paths.unlink(path)
            raise
        if self.listener is not None:
            self.sel.unregister(self.listener)
            self.listener.close()
        self.listener, self.sock_path = listener, path
        self.sel.register(listener, selectors.EVENT_READ, self._on_accept)

    def _check_files(self) -> None:
        """Keep the session reachable by name, or end it once the name is lost.

        Programs that clean /tmp may remove the socket of a long session, and
        a user may delete the run dir.
        """
        try:
            held = os.fstat(self.lock_fd)
            if _file_id(self.lock_path) != (held.st_dev, held.st_ino):
                # A new host may own the name now, so leave its files alone.
                self.name_lost = True
                self.begin_stop(DEFAULT_GRACE)
                return
            if _file_id(self.sock_path) != self.sock_id:
                self._listen()
                if self.meta["socket"] != str(self.sock_path):
                    self.meta["socket"] = str(self.sock_path)
                    paths.write_private(self.meta_path, self.meta)
            if not self.meta_path.exists():
                paths.write_private(self.meta_path, self.meta)
            self.check_error = ""
        except OSError as exc:
            error = f"burner: cannot check the session's files: {exc}"
            if error != self.check_error:
                print(error, file=sys.stderr, flush=True)
            self.check_error = error

    def _spawn(self) -> None:
        argv = [str(a) for a in self.req["argv"]]
        cwd = str(self.req["cwd"])
        env = {str(k): str(v) for k, v in self.req["env"].items()}
        terminal = self.terminal

        def child_setup() -> None:
            # Ignored signals stay ignored across exec, so restore them.
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if terminal:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        if terminal:
            from .screen import Screen

            self.screen = Screen(cols=self.cols, rows=self.rows)
            self.master, slave = os.openpty()
            self._set_winsize(self.cols, self.rows)
            stdio: dict[str, Any] = {"stdin": slave, "stdout": slave, "stderr": slave}
        else:
            # The child writes to a pipe, and the host copies it to the log, so the
            # host can move the log when it grows. Both ends are not inheritable.
            self.log_pipe, slave = os.pipe()
            stdio = {"stdin": subprocess.DEVNULL, "stdout": slave, "stderr": subprocess.STDOUT}
        try:
            # The child holds the lock too, so its name stays taken while it lives,
            # even when the host dies.
            child = subprocess.Popen(
                argv, cwd=cwd, env=env, start_new_session=True, preexec_fn=child_setup,
                umask=self.umask, pass_fds=(self.lock_fd,), **stdio,
            )
        except OSError as exc:
            where = exc.filename if exc.filename is not None else argv[0]
            raise StartError(f"cannot run {argv[0]}: {exc.strerror}: {where}") from None
        finally:
            if slave >= 0:
                os.close(slave)
        # Track the child at once, so a failure below still ends it.
        self.child = child
        self.exit_watch = ExitWatch(child.pid)
        if terminal:
            os.set_blocking(self.master, False)
            self.pty_open = True
            self.sel.register(self.master, selectors.EVENT_READ, self._on_pty)
        else:
            os.set_blocking(self.log_pipe, False)
            self.sel.register(self.log_pipe, selectors.EVENT_READ, self._on_log_pipe)

    # Main loop

    def run(self) -> None:
        while not self.done:
            for key, mask in self.sel.select(self._timeout()):
                key.data(mask)
            if self.term_requested:
                self.term_requested = False
                self.begin_stop(DEFAULT_GRACE)
            self._fire_timers()
            if self.lingering:
                if self.child.returncode is not None or not self.timers or self._group_empty():
                    self._reap()
                continue
            if self.exit_watch.check():
                self._finish()
                continue
            if not self.name_lost and time.monotonic() >= self.next_check:
                self.next_check = time.monotonic() + self.check_seconds
                self._check_files()
            now = time.monotonic()
            for c in list(self.clients):
                if c is not self.attached and now - c.last > IDLE_CLIENT_SECONDS:
                    self._drop(c)

    def _timeout(self) -> float | None:
        """Seconds until the next thing to do. Signals and I/O wake the loop sooner."""
        now = time.monotonic()
        due = list(self.timers.values())
        if self.lingering:
            due.append(now + LINGER_POLL)
        elif not self.name_lost:
            due.append(self.next_check)
        due += [c.last + IDLE_CLIENT_SECONDS for c in self.clients if c is not self.attached]
        return max(0.0, min(due) - now) if due else None

    def _on_sigterm(self, *_: Any) -> None:
        self.term_requested = True

    def _on_wake(self, mask: int) -> None:
        try:
            while self.wake_r.recv(4096):
                pass
        except OSError:
            pass

    def begin_stop(self, grace: float) -> None:
        """Start the stop ladder. A later stop with a shorter grace moves it earlier.

        A log session gets SIGTERM, then SIGKILL at grace. A terminal session
        gets SIGHUP, as from a closing terminal, then SIGTERM at grace/2, then
        SIGKILL at grace.
        """
        now = time.monotonic()
        if self.terminal:
            first = signal.SIGHUP
            later = {signal.SIGTERM: now + grace / 2, signal.SIGKILL: now + grace}
        else:
            first, later = signal.SIGTERM, {signal.SIGKILL: now + grace}
        if self.stopping:
            for sig in self.timers:
                self.timers[sig] = min(self.timers[sig], later[sig])
            return
        self.stopping = True
        self._signal_group(first)
        self.timers = later

    def _fire_timers(self) -> None:
        now = time.monotonic()
        for sig, due in sorted(self.timers.items(), key=lambda item: item[1]):
            if due <= now:
                del self.timers[sig]
                self._signal_group(sig)

    def _signal_group(self, sig: int) -> None:
        # The child is not reaped yet, so its pid still names its group.
        if self.child.returncode is None:
            # A group left with only a zombie may report either error.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.child.pid, sig)

    def _group_empty(self) -> bool:
        """True when no process but the unreaped child is left in its group."""
        try:
            os.killpg(self.child.pid, 0)
        except (ProcessLookupError, PermissionError):
            return True  # macOS reports an error for a group that holds only a zombie.
        # Linux counts the zombie as a member, so look at the other processes.
        return sys.platform == "linux" and not _group_members(self.child.pid)

    def _finish(self) -> None:
        """The child exited: report how, free the name, then finish its group."""
        # Keep what the child wrote last, but do not wait for processes that hold
        # the output open after it exited.
        if self.pty_open:
            _drain(self._read_pty)
        if self.log_pipe >= 0:
            _drain(self._read_log_pipe)
        self._close_log()
        self.lingering = True
        if not self.stopping:
            self.begin_stop(DEFAULT_GRACE)
        if self.exit_watch.status is not None:
            self._report_end(self.exit_watch.status)
        # Otherwise the child exited before the watch began, and only a reap tells
        # how. The host reports the end once the group is finished, so the child
        # stays unreaped and keeps its process group id until then.

    def _report_end(self, status: dict[str, int]) -> None:
        """Write the exit record, tell an attached client, and free the name."""
        status = {**status, "time": time.time(), "log": str(self.log)}
        if self.error:
            status["error"] = self.error
        if not self.name_lost:
            with contextlib.suppress(OSError):
                paths.write_private(self.exit_path, status)
        if self.attached:
            self._send(self.attached, protocol.message({"event": "exit", **status}))
        self._close_clients()
        self._remove_files()
        self.reported = True

    def _close_log(self) -> None:
        """Stop copying output to the log once the child exited.

        The name may start again at once with a new host and a new log, so
        what is left of the group gets EPIPE from now on.
        """
        if self.log_pipe >= 0:
            self.sel.unregister(self.log_pipe)
            os.close(self.log_pipe)
            self.log_pipe = -1
        if self.log_fd >= 0:
            if self.log_to_stderr:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2)
                os.close(devnull)
                self.log_to_stderr = False
            os.close(self.log_fd)
            self.log_fd = -1

    def _reap(self) -> None:
        code = self.child.wait()
        if not self.reported:
            self._report_end({"code": code} if code >= 0 else {"signal": -code})
        self.exit_watch.close()
        self.done = True

    def _close_clients(self) -> None:
        for c in list(self.clients):
            try:
                if c.outbuf:
                    c.sock.settimeout(1.0)
                    c.sock.sendall(c.outbuf)
            except OSError:
                pass
            c.sock.close()
        self.clients.clear()
        self.attached = None
        if self.listener is not None:
            self.sel.unregister(self.listener)
            self.listener.close()
            self.listener = None

    def abort(self, error: str) -> None:
        """End the session after an unexpected error in the host."""
        self.error = error
        self.stopping, self.timers = True, {}
        self._signal_group(signal.SIGKILL)
        deadline = time.monotonic() + 5
        while not self.exit_watch.check() and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.exit_watch.exited and not self.lingering:
            self._finish()
        elif not self.exit_watch.exited:
            self._close_clients()
            self._remove_files()
        # Otherwise the end was reported, or the reap below reports it.
        if self.exit_watch.exited:
            self._reap()

    # PTY

    def _set_winsize(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.pty_size = (cols, rows)

    def _resize(self, cols: int, rows: int, repaint: bool) -> None:
        """Size the PTY. With repaint, make the child redraw even at the same size."""
        if not self.pty_open:
            return
        if (cols, rows) != self.pty_size:
            self._set_winsize(cols, rows)
        elif repaint:
            self._signal_group(signal.SIGWINCH)

    def _on_pty(self, mask: int) -> None:
        if mask & selectors.EVENT_READ:
            self._read_pty()
        if mask & selectors.EVENT_WRITE and self.pty_open:
            self._flush_pty()

    def _read_pty(self) -> int:
        """Read once from the PTY. Returns the byte count, 0 when nothing came."""
        try:
            data = os.read(self.master, PTY_READ_SIZE)
        except BlockingIOError:
            return 0
        except OSError as exc:
            # Linux reports EIO on the master once the last slave fd closes.
            if exc.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self.sel.unregister(self.master)
            self.pty_open = False
            return 0
        try:
            self.screen.feed(data)
        except Exception as exc:
            print(f"burner: the screen was reset after an error: {exc!r}", file=sys.stderr,
                  flush=True)
            self.screen = type(self.screen)(self.screen.cols, self.screen.rows)
        if self.attached:
            self._send(self.attached, protocol.frame(protocol.DATA, data))
        else:
            scan = self.query_tail + data
            reply = answer_queries(scan, self.screen.cursor)
            # A child that asks without reading the answers gets no more of them.
            if reply and len(self.pty_out) <= QUERY_REPLY_LIMIT:
                self._write_pty(reply)
            start = scan.rfind(b"\x1b")
            partial = start >= 0 and _PARTIAL_QUERY.fullmatch(scan, start)
            self.query_tail = scan[start:] if partial else b""
        return len(data)

    def _write_pty(self, data: bytes) -> None:
        self.pty_out += data
        self._flush_pty()

    def _flush_pty(self) -> None:
        try:
            written = os.write(self.master, self.pty_out)
        except BlockingIOError:
            written = 0
        except OSError:
            self.pty_out.clear()
            written = 0
        del self.pty_out[:written]
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if self.pty_out else 0)
        self.sel.modify(self.master, events, self._on_pty)

    # Clients

    def _on_accept(self, mask: int) -> None:
        try:
            sock, _ = self.listener.accept()
        except OSError:
            return
        sock.setblocking(False)
        c = Client(sock)
        self.clients[c] = lambda m: self._on_client(c, m)
        self.sel.register(sock, selectors.EVENT_READ, self.clients[c])

    def _on_client(self, c: Client, mask: int) -> None:
        try:
            self._serve(c, mask)
        except Exception as exc:
            print(f"burner: dropped a client after an error: {exc!r}", file=sys.stderr, flush=True)
            self._drop(c)

    def _serve(self, c: Client, mask: int) -> None:
        if c not in self.clients:
            return
        if mask & selectors.EVENT_WRITE:
            self._flush(c)
        if not mask & selectors.EVENT_READ or c not in self.clients:
            return
        try:
            data = c.sock.recv(READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(c)
            return
        if c.closing:
            return  # Nothing more is read from a client that is closing.
        c.last = time.monotonic()
        c.inbuf += data
        while c in self.clients and not c.closing:
            try:
                got = protocol.parse(c.inbuf)
            except protocol.ProtocolError as exc:
                c.closing = True
                self._send(c, protocol.message({"ok": False, "error": str(exc)}))
                return
            if got is None:
                return
            kind, value = got
            if kind == protocol.JSON:
                self._request(c, value)
            elif c is self.attached and self.pty_open:
                # Typed input past the limit is dropped, as the write op refuses it.
                if len(self.pty_out) + len(value) <= PTY_INPUT_LIMIT:
                    self._write_pty(value)
            else:
                self._send(c, protocol.message({"ok": False, "error": "unexpected data frame"}))

    def _send(self, c: Client, data: bytes) -> None:
        if c not in self.clients:
            return
        c.outbuf += data
        if len(c.outbuf) > CLIENT_BUFFER_LIMIT:
            self._drop(c)
        else:
            self._flush(c)

    def _flush(self, c: Client) -> None:
        try:
            sent = c.sock.send(c.outbuf) if c.outbuf else 0
        except BlockingIOError:
            sent = 0
        except OSError:
            self._drop(c)
            return
        del c.outbuf[:sent]
        if c.closing and not c.outbuf:
            self._drop(c)
        elif c.writing != bool(c.outbuf):
            c.writing = bool(c.outbuf)
            events = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.writing else 0)
            self.sel.modify(c.sock, events, self.clients[c])

    def _drop(self, c: Client) -> None:
        if c not in self.clients:
            return
        del self.clients[c]
        self.sel.unregister(c.sock)
        c.sock.close()
        if c is self.attached:
            self.attached = None
            self._restore_size()

    def _restore_size(self) -> None:
        if self.terminal:
            self.screen.resize(self.cols, self.rows)
            self._resize(self.cols, self.rows, repaint=True)

    # Requests

    def _request(self, c: Client, req: dict[str, Any]) -> None:
        ops = {
            "info": self._op_info,
            "screen": self._op_screen,
            "write": self._op_write,
            "stop": self._op_stop,
            "attach": self._op_attach,
            "resize": self._op_resize,
            "detach": self._op_detach,
        }
        op = req.get("op")
        handler = ops.get(op) if isinstance(op, str) else None
        if handler is None:
            self._send(c, protocol.message({"ok": False, "error": f"unknown op: {op!r}"}))
            return
        if op not in ("info", "stop") and not self.terminal:
            self._send(c, protocol.message({"ok": False, "error": "not a terminal session"}))
            return
        try:
            result = handler(c, req)
        except Exception as exc:
            result = {"ok": False, "error": f"bad request: {exc}"}
        if result is not None:
            self._send(c, protocol.message({"ok": True, **result}))

    def _op_info(self, c: Client, req: dict[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self.name,
            "pid": os.getpid(),
            "child": self.child.pid,
            "terminal": self.terminal,
            "attached": self.attached is not None,
        }
        if self.terminal:
            info.update(
                cols=self.pty_size[0],
                rows=self.pty_size[1],
                application_cursor_keys=bool(self.screen.application_cursor_keys),
                bracketed_paste=bool(self.screen.bracketed_paste),
                alternate=bool(self.screen.alternate),
            )
        return info

    def _op_screen(self, c: Client, req: dict[str, Any]) -> None:
        lines = req.get("lines")
        if lines is not None and (not isinstance(lines, int) or lines < 0):
            raise ValueError("lines must be a non-negative integer")
        if c in self.clients:
            # Built in place: a large screen is one of the biggest replies.
            protocol.append_text_reply(c.outbuf, self.screen.text_rows(lines))
            self._send(c, b"")

    def _op_write(self, c: Client, req: dict[str, Any]) -> dict[str, Any]:
        data = base64.b64decode(req["data"], validate=True)
        if not self.pty_open:
            return {"ok": False, "error": "the session's terminal is closed"}
        if len(self.pty_out) + len(data) > PTY_INPUT_LIMIT:
            return {"ok": False, "error": "input buffer is full"}
        self._write_pty(data)
        return {}

    def _op_stop(self, c: Client, req: dict[str, Any]) -> dict[str, Any]:
        self.begin_stop(_grace(req.get("grace", DEFAULT_GRACE)))
        return {}

    def _op_attach(self, c: Client, req: dict[str, Any]) -> None:
        cols, rows = _size(req)
        old = self.attached
        if old is not None:
            self.attached = None
            old.closing = True
            reason = "another client attached"
            self._send(old, protocol.message({"event": "detached", "reason": reason}))
        self.attached = c
        self.query_tail = b""
        self.screen.resize(cols, rows)
        self._send(c, protocol.message({"ok": True}))
        self._send(c, protocol.frame(protocol.DATA, self.screen.replay()))
        self._resize(cols, rows, repaint=True)

    def _op_resize(self, c: Client, req: dict[str, Any]) -> None:
        if c is self.attached:
            cols, rows = _size(req)
            self.screen.resize(cols, rows)
            self._resize(cols, rows, repaint=False)

    def _op_detach(self, c: Client, req: dict[str, Any]) -> None:
        c.closing = True
        self._flush(c)


def _open_append(path: Path) -> tuple[int, bool]:
    """Open a log for append, and tell whether it was created.

    A new file gets mode 0600; an existing one keeps its mode.
    """
    flags = os.O_WRONLY | os.O_APPEND
    while True:
        try:
            try:
                return paths.open_own(path, flags), False
            except FileNotFoundError:
                return paths.open_own(path, flags | os.O_CREAT | os.O_EXCL), True
        except FileExistsError:
            continue  # Created by another process since the first try.
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EPERM, errno.ENXIO, errno.EMLINK):
                raise StartError(f"log {path} is not a regular file owned by this user") from None
            raise


def _status(wait_status: int) -> dict[str, int]:
    if os.WIFSIGNALED(wait_status):
        return {"signal": os.WTERMSIG(wait_status)}
    return {"code": os.WEXITSTATUS(wait_status)}


def _group_members(pgid: int) -> bool:
    """True when a live process other than pgid itself is in process group pgid (Linux)."""
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == pgid:
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as f:
                data = f.read()
        except OSError:
            continue  # The process ended.
        # The name in parentheses may hold spaces, so read the fields after the last ")".
        fields = data[data.rindex(b")") + 2 :].split()
        state, pgrp = fields[0], int(fields[2])
        if pgrp == pgid and state != b"Z":
            return True
    return False


def _drain(read: Any) -> None:
    """Call read until it returns 0 or DRAIN_LIMIT bytes came."""
    drained = 0
    while drained < DRAIN_LIMIT and (got := read()):
        drained += got


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _file_id(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return st.st_dev, st.st_ino


def _size(req: dict[str, Any]) -> tuple[int, int]:
    """The size an attached terminal asks for, clamped to the limits so it still attaches."""
    cols, rows = req["cols"], req["rows"]
    if type(cols) is not int or type(rows) is not int or cols < 1 or rows < 1:
        raise ValueError("terminal size must be positive integers")
    cols = min(cols, paths.MAX_SIDE)
    return cols, min(rows, paths.MAX_SIDE, paths.MAX_CELLS // cols)


def _grace(value: Any) -> float:
    if type(value) not in (int, float):
        raise TypeError("grace must be a number")
    try:
        grace = float(value)
    except OverflowError:
        grace = math.inf
    if not math.isfinite(grace) or grace < 0:
        raise ValueError("grace must be a finite number >= 0")
    return grace


def _read_start_request() -> dict[str, Any]:
    buf = bytearray()
    while True:
        got = protocol.parse(buf, protocol.MAX_START_FRAME)
        if got is not None:
            return got[1]
        chunk = os.read(0, 1 << 20)
        if not chunk:
            raise StartError("the start request was cut short")
        buf += chunk


def _report(reply: dict[str, Any]) -> bool:
    """Write the reply for the starter. False when the starter is gone."""
    try:
        os.write(1, (json.dumps({"v": protocol.VERSION, **reply}) + "\n").encode())
    except OSError:
        return False
    return True


USAGE = "usage: python -m burner.host RUN_DIR NAME, with the start request on stdin"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not paths.NAME_RE.fullmatch(args[1]):
        print(USAGE, file=sys.stderr)
        return 2
    run_dir, name = Path(args[0]), args[1]
    # Fork once more so the starter never has to reap the host.
    if os.fork():
        os._exit(0)
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_IGN)
    umask = os.umask(0o077)
    try:
        host = SessionHost(run_dir, name, _read_start_request(), umask)
        host.setup()
    except Exception as exc:
        known = (StartError, HostError, OSError, TypeError, ValueError)
        message = str(exc) if isinstance(exc, known) else repr(exc)
        _report({"ok": False, "error": message})
        return 1
    if not _report({"ok": True, "pid": os.getpid(), "child": host.child.pid}):
        # The starter cannot learn that the session started, so it did not.
        host.undo_start()
        return 1
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    # Host errors go to the log of a log session. A terminal session has no log.
    os.dup2(devnull if host.terminal else host.log_fd, 2)
    host.log_to_stderr = not host.terminal
    os.close(devnull)
    try:
        host.run()
    except BaseException as exc:
        traceback.print_exc()
        host.abort("".join(traceback.format_exception_only(exc)).strip())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
