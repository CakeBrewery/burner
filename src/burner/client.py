"""Start, inspect, drive, and stop sessions in a run dir."""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from . import keys, paths, protocol
from .errors import AlreadyRunningError, HostError, NotRunningError

SEND_LINE_DELAY = 0.05
STOP_MARGIN = 2.0
WAIT_INTERVAL = 0.05
STOP_REQUEST_TIMEOUT = 2.0
WRITE_CHUNK = 256 * 1024
TAIL_BLOCK = 64 * 1024
TAIL_MAX = 8 << 20
# The tests set a shorter file check for the hosts they start. None keeps the host's own.
_FILE_CHECK_SECONDS: float | None = None
# The host gets only what it needs to import this package, not the caller's environment.
HOST_ENV_KEYS = ("PATH", "PYTHONPATH")


@dataclass(frozen=True)
class Session:
    """A running session.

    ``pid`` is the child's pid, which also names its process group.
    ``started`` is the start time in seconds since the epoch.
    """

    name: str
    pid: int
    terminal: bool
    started: float


@dataclass(frozen=True)
class ExitStatus:
    """How a session's child ended.

    ``code`` is its exit code, or None when a signal ended it. ``signal`` is
    that signal's number, or None. ``ended`` is the end time in seconds
    since the epoch.
    """

    code: int | None
    signal: int | None
    ended: float


T = TypeVar("T")


def _os_errors(method: Callable[..., T]) -> Callable[..., T]:
    """Report an OSError from the method as a HostError."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return method(*args, **kwargs)
        except OSError as exc:
            raise HostError(str(exc)) from exc

    return wrapper


class Host:
    """Sessions in one run dir. Each session has its own host process.

    ``run_dir`` defaults to ``$BURNER_DIR``, else ``~/.burner``. Making a
    Host creates nothing on disk; the first start creates the run dir.
    ``timeout`` bounds each request to a session's host, in seconds.
    """

    def __init__(
        self, run_dir: str | os.PathLike[str] | None = None, *, timeout: float = 15.0
    ) -> None:
        if run_dir is None:
            run_dir = os.environ.get("BURNER_DIR") or "~/.burner"
        self.run_dir = Path(run_dir).expanduser().absolute()
        if _number("timeout", timeout) <= 0:
            raise ValueError("timeout must be greater than 0")
        self.timeout = float(timeout)

    # Lifecycle

    @_os_errors
    def start(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        terminal: bool = False,
        cols: int = 80,
        rows: int = 24,
        log: str | os.PathLike[str] | None = None,
    ) -> Session:
        """Start PROGRAM as session NAME and return it once the child runs.

        ``cwd`` defaults to the caller's working directory. ``env`` is the
        child's whole environment; None passes the caller's. A terminal
        session runs on a PTY of ``cols`` by ``rows``. A log session writes
        to ``log``, by default ``RUN_DIR/NAME.log``.
        """
        _check_name(name)
        if isinstance(argv, str) or not isinstance(argv, Sequence):
            raise TypeError("argv must be a sequence of strings")
        if not argv:
            raise ValueError("argv must not be empty")
        args = [os.fspath(a) if isinstance(a, os.PathLike) else a for a in argv]
        if not all(isinstance(a, str) for a in args):
            raise TypeError("argv must be a sequence of strings")
        if any("\0" in a for a in args):
            raise ValueError("argv must not hold a NUL character")
        cwd = os.getcwd() if cwd is None else _path_arg("cwd", cwd)
        if env is None:
            env = os.environ
        elif not isinstance(env, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise TypeError("env must map strings to strings")
        for key, value in env.items():
            if not key or "=" in key or "\0" in key or "\0" in value:
                raise ValueError(f"env holds an invalid variable {key!r}")
        if not isinstance(terminal, bool):
            raise TypeError(f"terminal must be a bool, not {type(terminal).__name__}")
        paths.check_size(cols, rows)
        if log is not None and terminal:
            raise ValueError("log applies only to log sessions, not terminal sessions")
        if self.alive(name):
            raise AlreadyRunningError(f"session {name} is already running")
        request = {
            "argv": args,
            "cwd": cwd,
            "env": dict(env),
            "terminal": terminal,
            "cols": cols,
            "rows": rows,
            "log": None if log is None else _path_arg("log", log),
        }
        if _FILE_CHECK_SECONDS is not None:
            request["_check_seconds"] = _FILE_CHECK_SECONDS
        payload = protocol.frame(protocol.JSON, json.dumps(request).encode())
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "burner.host", str(self.run_dir), name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env={k: os.environ[k] for k in HOST_ENV_KEYS if k in os.environ},
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(payload, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            if self.alive(name):
                self.stop(name, grace=1.0)
            raise HostError(f"session {name} did not start within {self.timeout:g}s") from None
        except BrokenPipeError:
            out, err = proc.communicate()
        line = out.decode(errors="replace").strip().splitlines()
        reply = json.loads(line[0]) if line and line[0].startswith("{") else {}
        if not reply.get("ok"):
            if self.alive(name):
                raise AlreadyRunningError(f"session {name} is already running")
            detail = reply.get("error") or _last_line(err) or "the host exited early"
            raise HostError(f"cannot start session {name}: {detail}")
        meta = self._meta(name)
        if meta is not None and meta.get("child") == reply.get("child"):
            session = _session(name, meta)
            if session is not None:
                return session
        # The child already ended and its host removed the meta file.
        return Session(name, int(reply["child"]), bool(terminal), time.time())

    def alive(self, name: str) -> bool:
        """True when the session is known to be running."""
        return paths.lock_probe(self._path(name, ".lock")) is True

    def gone(self, name: str) -> bool:
        """True when the session is known not to be running.

        When the check fails, both ``alive`` and ``gone`` are false.
        """
        return paths.lock_probe(self._path(name, ".lock")) is False

    def stop(self, name: str, *, grace: float = 5.0) -> None:
        """End the session and return once it is gone. Does nothing when it is gone.

        The child's process group gets a polite signal first and SIGKILL
        after ``grace`` seconds.
        """
        _number("grace", grace)
        if self.gone(name):
            return
        try:
            timeout = min(self.timeout, STOP_REQUEST_TIMEOUT)
            self._call(name, {"op": "stop", "grace": grace}, timeout=timeout)
        except HostError:
            pass
        else:
            if self._wait_gone(name, grace + STOP_MARGIN):
                return
        # The host did not answer or did not finish, so signal from here.
        started = time.monotonic()
        for delay, sig in (
            ((0.0, signal.SIGHUP), (grace / 2, signal.SIGTERM), (grace, signal.SIGKILL))
            if (self._meta(name) or {}).get("terminal")
            else ((0.0, signal.SIGTERM), (grace, signal.SIGKILL))
        ):
            if self._wait_gone(name, started + delay - time.monotonic()):
                return
            self._signal(name, sig)
        if not self._wait_gone(name, STOP_MARGIN):
            raise HostError(f"session {name} is still running after stop")

    @_os_errors
    def sessions(self) -> list[Session]:
        """The running sessions, sorted by name."""
        found = []
        for lock in sorted(self.run_dir.glob("*.lock")):
            name = lock.name[: -len(".lock")]
            if not paths.NAME_RE.fullmatch(name) or paths.lock_probe(lock) is not True:
                continue
            session = _session(name, self._meta(name))
            if session is not None:
                found.append(session)
        return sorted(found, key=lambda s: s.name)

    def wait(self, name: str, timeout: float | None = None) -> ExitStatus | None:
        """Wait until the session is gone and return how it ended.

        Returns None when no exit record exists. Raises TimeoutError when
        the session still runs after ``timeout`` seconds, and HostError when
        the check whether it runs keeps failing.
        """
        seconds = math.inf if timeout is None else _number("timeout", timeout)
        deadline = time.monotonic() + seconds
        unknown = 0
        while not self.gone(name):
            if self.alive(name):
                unknown = 0
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"session {name} is still running after {seconds:g}s")
            else:
                # Neither gone nor alive twice in a row: the check itself fails.
                unknown += 1
                if unknown >= 2:
                    raise HostError(f"cannot tell whether session {name} is running")
            time.sleep(WAIT_INTERVAL)
        return self.exit_status(name)

    def exit_status(self, name: str) -> ExitStatus | None:
        """How the last child of this name ended, or None when no record exists."""
        return _exit_status(paths.read_json(self._path(name, ".exit")))

    # Terminal sessions

    def screen(self, name: str, lines: int | None = None) -> str:
        """The text on a terminal session's screen, or its last ``lines`` rows.

        Trailing blank rows and trailing spaces are left out.
        """
        if lines is not None:
            _count("lines", lines)
        self._terminal(name)
        return str(self._call(name, {"op": "screen", "lines": lines})["text"])

    def press(self, name: str, *keys_: str) -> None:
        """Press keys in a terminal session, for example ``Up``, ``Enter``, ``C-c``."""
        for key in keys_:
            if not isinstance(key, str):
                raise TypeError(f"key must be a str, not {type(key).__name__}")
            keys.encode(key)
        self._terminal(name)
        info = self._call(name, {"op": "info"})
        app = bool(info.get("application_cursor_keys"))
        self.write(name, b"".join(keys.encode(key, app) for key in keys_))

    def send(self, name: str, text: str) -> None:
        """Type one line of text into a terminal session, then press Enter."""
        if not isinstance(text, str):
            raise TypeError(f"text must be a str, not {type(text).__name__}")
        if "\n" in text or "\r" in text:
            raise ValueError("text must not contain a newline")
        self.write(name, text.encode())
        time.sleep(SEND_LINE_DELAY)
        self.write(name, b"\r")

    def write(self, name: str, data: bytes) -> None:
        """Write raw bytes to a terminal session's input."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"data must be bytes, not {type(data).__name__}")
        data = bytes(data)
        self._terminal(name)
        for i in range(0, len(data), WRITE_CHUNK):
            chunk = base64.b64encode(data[i : i + WRITE_CHUNK]).decode()
            self._call(name, {"op": "write", "data": chunk})

    @_os_errors
    def attach(self, name: str) -> int:
        """Connect this terminal to the session until detach (Ctrl-\\) or its end.

        Returns 0 after a detach, else the child's exit code, or 128 plus
        the signal number when a signal ended it.
        """
        from .attach import attach

        meta = self._terminal(name)
        code = attach(meta["socket"], self.timeout)
        if code is not None:
            return code
        self._wait_gone(name, STOP_MARGIN)
        status = self.exit_status(name)
        if status is None:
            raise HostError(f"session {name} ended without an exit record")
        return _exit_code(status)

    # Logs

    def log_path(self, name: str) -> Path:
        """The path of the session's log file, also after its end."""
        for record in (self._meta(name), paths.read_json(self._path(name, ".exit"))):
            if record and isinstance(record.get("log"), str):
                return Path(record["log"])
        return self._path(name, ".log")

    @_os_errors
    def tail(self, name: str, lines: int = 30) -> str:
        """The last ``lines`` lines of a session's log, also after its end."""
        if _count("lines", lines) == 0:
            return ""
        try:
            data = _read_end(self.log_path(name), lines)
        except FileNotFoundError:
            return ""
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])

    # Internals

    def _path(self, name: str, suffix: str) -> Path:
        return self.run_dir / (_check_name(name) + suffix)

    def _meta(self, name: str) -> dict[str, Any] | None:
        meta = paths.read_json(self._path(name, ".json"))
        if meta is None or not isinstance(meta.get("socket"), str):
            return None
        for key in ("pid", "child", "pgid", "sid"):
            value = meta.get(key)
            if key == "sid" and value is None:
                continue  # An older host records no session id.
            if type(value) is not int or value <= 1:
                return None
        return meta

    def _terminal(self, name: str) -> dict[str, Any]:
        meta = self._meta(name)
        if meta is None or not self.alive(name):
            raise NotRunningError(f"session {name} is not running")
        if not meta.get("terminal"):
            raise HostError(f"session {name} is a log session; read it with tail")
        return meta

    def _call(self, name: str, request: dict[str, Any], timeout: float | None = None) -> dict:
        meta = self._meta(name)
        if meta is None or not self.alive(name):
            raise NotRunningError(f"session {name} is not running")
        timeout = self.timeout if timeout is None else timeout
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(meta["socket"])
            sock.sendall(protocol.message(request))
            kind, reply = protocol.read_frame(sock, bytearray())
        except TimeoutError:
            raise HostError(f"session {name} did not answer within {timeout:g}s") from None
        except (OSError, EOFError, protocol.ProtocolError) as exc:
            # The session may have ended during the request.
            if not self.alive(name):
                raise NotRunningError(f"session {name} is not running") from None
            raise HostError(f"session {name}: {exc}") from None
        finally:
            sock.close()
        if kind != protocol.JSON or not reply.get("ok"):
            raise HostError(f"session {name}: {reply.get('error', 'bad reply')}")
        return reply

    def _wait_gone(self, name: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.gone(name):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _signal(self, name: str, sig: int) -> None:
        """Signal the child's group, and the host on SIGKILL, while the lock is held.

        The held lock proves the pids in the meta file still belong to this
        session while the host lives. After the host died, the child's group
        and the host's session id tell whether the pids are still its own.
        """
        meta = self._meta(name)
        if meta is None or not self.alive(name):
            return
        pgid, host_pid, sid = meta["pgid"], meta["pid"], meta.get("sid")
        if sid is None:  # An older host records no session id.
            targets = [(os.killpg, pgid)] + ([(os.kill, host_pid)] if sig == signal.SIGKILL else [])
            for send, pid in targets:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    send(pid, sig)
            return
        try:
            host_alive = os.getsid(host_pid) == sid
        except (ProcessLookupError, PermissionError):
            host_alive = False
        if host_alive and sig == signal.SIGKILL:
            with contextlib.suppress(ProcessLookupError):
                os.kill(host_pid, sig)
        # The child leads its own session. Once the leader is gone, nothing
        # reserves the group id, except members that are still in the group.
        try:
            group_ours = os.getsid(pgid) == pgid
        except ProcessLookupError:
            try:
                os.killpg(pgid, 0)
                group_ours = True
            except OSError:
                group_ours = False
        except PermissionError:
            group_ours = False
        if group_ours:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, sig)
        elif not host_alive:
            raise HostError(
                f"session {name}: {name}.lock is held by a process outside the session's group"
            )

    @_os_errors
    def clean(self, name: str | None = None) -> list[str]:
        """Remove the files of ended sessions, or of one name, and return the names cleaned.

        A running session keeps its files; naming one raises HostError. A log
        chosen with ``log=`` and the run dir itself always stay.
        """
        from .cleanup import clean

        return clean(self.run_dir, None if name is None else _check_name(name))


def _check_name(name: object) -> str:
    if not isinstance(name, str):
        raise TypeError(f"session name must be a str, not {type(name).__name__}")
    if not paths.NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid session name {name!r}: use 1 to 100 letters, digits, "
            "'.', '_', '-', starting with a letter or digit"
        )
    return name


def _path_arg(what: str, value: object) -> str:
    """An absolute path from a str or path object."""
    path = os.fspath(value) if isinstance(value, os.PathLike) else value
    if not isinstance(path, str):
        raise TypeError(f"{what} must be a str or path, not {type(value).__name__}")
    if "\0" in path:
        raise ValueError(f"{what} must not hold a NUL character")
    return os.path.abspath(path)


def _count(what: str, value: object) -> int:
    """An int >= 0, for line counts."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int, not {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{what} must be >= 0, not {value}")
    return value


def _number(what: str, value: object) -> float:
    """A finite number >= 0, for timeouts and grace periods."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be a number, not {type(value).__name__}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{what} must be a finite number >= 0, not {value}")
    return float(value)


def _session(name: str, meta: dict[str, Any] | None) -> Session | None:
    """Read a session from its meta file. A malformed one counts as missing."""
    if meta is None:
        return None
    started = meta.get("started")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    try:
        datetime.fromtimestamp(started)  # ls shows it as a local time.
        return Session(name, int(meta["child"]), bool(meta["terminal"]), float(started))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _exit_status(record: dict[str, Any] | None) -> ExitStatus | None:
    """Read an exit record written by the host. A malformed record counts as missing."""
    if record is None:
        return None
    code, sig, ended = record.get("code"), record.get("signal"), record.get("time")
    if not isinstance(ended, (int, float)) or isinstance(ended, bool):
        return None
    for value in (code, sig):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            return None
    if (code is None) == (sig is None):
        return None
    return ExitStatus(code, sig, float(ended))


def _exit_code(status: ExitStatus) -> int:
    """The shell's view of an exit: the code, or 128 plus the signal number."""
    return status.code if status.code is not None else 128 + int(status.signal or 0)


def _read_end(path: Path, lines: int) -> bytes:
    """Read enough of the end of a file to hold the last lines, never all of a big one."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        block = TAIL_BLOCK
        while True:
            start = max(0, size - block)
            f.seek(start)
            data = f.read(size - start)
            if start == 0 or data.count(b"\n") > lines or block >= TAIL_MAX:
                return data
            block *= 4


def _last_line(data: bytes) -> str:
    lines = data.decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else ""
