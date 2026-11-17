"""Files in a run dir, shared by the host process and its clients.

For a session NAME the run dir holds ``NAME.lock`` (held by the host for its
whole life), ``NAME.json`` (meta, removed when the host exits), ``NAME.exit``
(how the child ended), ``NAME.log`` (default log), and usually
``NAME.sock``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .errors import UnsafeRunDirError

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# sun_path holds 104 bytes on macOS and 108 on Linux, including the final NUL.
MAX_SOCKET_PATH = 100
# Terminal size limits. They bound the memory and time a screen update can take.
MAX_SIDE = 1000
MAX_CELLS = 100_000


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid session name: {name!r}")
    return name


def check_size(cols: object, rows: object) -> tuple[int, int]:
    """Return (cols, rows) when both are within the terminal size limits."""
    for what, value in (("cols", cols), ("rows", rows)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{what} must be an int, not {type(value).__name__}")
        if not 1 <= value <= MAX_SIDE:
            raise ValueError(f"{what} must be 1 to {MAX_SIDE}, not {value}")
    if cols * rows > MAX_CELLS:  # type: ignore[operator]
        raise ValueError(f"terminal size {cols}x{rows} is over {MAX_CELLS} cells")
    return cols, rows  # type: ignore[return-value]


def check_run_dir(path: Path) -> None:
    """Raise UnsafeRunDirError unless only this user can change the run dir.

    The run dir may be a symlink the owner made, so it is followed. A missing
    run dir passes: it holds nothing to trust yet.
    """
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise UnsafeRunDirError(f"cannot check run dir {path}: {exc.strerror}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeRunDirError(f"run dir {path} is not a directory")
    if st.st_uid != os.geteuid():
        raise UnsafeRunDirError(f"run dir {path} is not owned by this user")
    if st.st_mode & 0o022:
        raise UnsafeRunDirError(
            f"run dir {path} is writable by other users: run chmod go-w {path}"
        )
    # Another user who can write the parent could put a dir of their own in its place.
    for parent in {os.path.dirname(os.path.abspath(path)), os.path.dirname(os.path.realpath(path))}:
        try:
            mode = os.stat(parent).st_mode
        except OSError as exc:
            raise UnsafeRunDirError(f"cannot check {parent}: {exc.strerror}") from None
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise UnsafeRunDirError(
                f"run dir {path} is in {parent}, which other users can write: "
                f"run chmod go-w {parent}"
            )


def make_run_dir(path: Path) -> None:
    """Create the run dir and any missing parents with mode 0700, then check it."""
    missing = [p for p in (path, *path.parents) if not os.path.lexists(p)]
    for part in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(part, 0o700)
            os.chmod(part, 0o700)  # The umask may have removed bits the owner needs.
    check_run_dir(path)


def lock_file(path: Path, operation: int, create: bool = True) -> int:
    """Open path and flock it with operation. Returns the fd.

    The holder of a lock may unlink its file and then unlock it. A locker
    that opened the file before the unlink would hold a lock on a file no
    longer at path, so it checks the file after flock and tries again.
    Raises BlockingIOError while another process holds the lock on the file
    at path, and FileNotFoundError when there is no file and create is False.
    """
    flags = os.O_RDONLY | (os.O_CREAT if create else 0)
    while True:
        fd = open_own(path, flags)
        try:
            fcntl.flock(fd, operation)
            held = False
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                raise
            held = True
        if same_file(path, fd):
            if not held:
                return fd
            os.close(fd)
            raise BlockingIOError(errno.EWOULDBLOCK, "the lock is held", str(path))
        os.close(fd)


def same_file(path: Path, fd: int) -> bool:
    """True when the file at path is the open file fd."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino)


def lock_probe(path: Path) -> bool | None:
    """True when another process holds the lock, False when it is free.

    None when the probe itself failed, so the caller does not guess. The
    probe takes a shared lock, so two probes never see each other as a host,
    which holds the lock exclusively.
    """
    check_run_dir(path.parent)
    try:
        os.close(lock_file(path, fcntl.LOCK_SH | fcntl.LOCK_NB, create=False))
    except BlockingIOError:
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return False


def write_private(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to path with mode 0600, replacing it in one step."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError:
        # Left by an earlier process with this pid. Unlinking never follows a link.
        unlink(tmp)
        fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        unlink(tmp)
        raise


def open_own(path: Path | str, flags: int) -> int:
    """Open a regular file owned by this user without following a symlink.

    O_NONBLOCK keeps the open from waiting on a FIFO; the fd is blocking again
    once the file is known to be regular. Raises OSError otherwise.
    """
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        raise OSError(errno.EPERM, "not a regular file owned by this user", str(path))
    os.set_blocking(fd, True)
    return fd


def read_json(path: Path) -> dict[str, Any] | None:
    check_run_dir(path.parent)
    try:
        with os.fdopen(open_own(path, os.O_RDONLY)) as f:
            value = json.load(f)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_own_socket(path: str, run_dir: Path) -> bool:
    """True for a socket of this user in the run dir or in a private /tmp/burner-* dir."""
    try:
        st = os.lstat(path)
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
            return False
        parent = os.path.dirname(path)
        if parent == str(run_dir):
            return True
        return is_own_socket_dir(parent)
    except OSError:
        return False


def remove_socket(path: str, run_dir: Path) -> None:
    """Remove the socket of an earlier host, and its dir under /tmp once no host uses it."""
    if is_own_socket(path, run_dir):
        unlink(path)
    parent = os.path.dirname(path)
    if not is_own_socket_dir(parent):
        return
    try:
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        # A host holds a shared lock on its socket dir. rmdir takes only an empty dir.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.rmdir(parent)
    except OSError:
        pass
    finally:
        os.close(fd)


def is_own_socket_dir(path: str) -> bool:
    """True for a dir /tmp/burner-* of this user with mode 0700."""
    if os.path.dirname(path) != "/tmp" or not os.path.basename(path).startswith("burner-"):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid()
        and stat.S_IMODE(st.st_mode) == 0o700
    )


def unlink(path: Path | str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
