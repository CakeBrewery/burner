"""Remove the files that ended sessions leave in a run dir."""

from __future__ import annotations

import fcntl
import os
import re
import stat
from pathlib import Path

from . import paths
from .errors import HostError

# Files of a session NAME besides its lock, meta, and exit record, removed only
# for a name that a meta or exit record shows to be burner's.
OUTPUT_SUFFIXES = (".sock", ".log", ".log.1")
# Temp files that write_private makes: .NAME.json.PID.tmp and .NAME.exit.PID.tmp.
TEMP = re.compile(r"\.(?P<name>.+)\.(json|exit)\.[0-9]+\.tmp")


def clean(run_dir: Path, name: str | None = None) -> list[str]:
    """Remove the files of ended sessions in run_dir, or of one name. Returns the names cleaned.

    A file counts as burner's only on proof: an empty lock file, a meta record,
    or an exit record of this user. The socket and the logs of a name go only
    when a meta or exit record proves the name is a session. A name is cleaned only while this
    process holds its lock, so a running or starting session keeps its files.
    A custom log and the run dir itself always stay.
    """
    paths.check_run_dir(run_dir)
    if not run_dir.is_dir():
        return []
    if name is not None:
        paths.check_name(name)
        try:
            return [name] if _clean(run_dir, name, _custom_logs(run_dir), must=True) else []
        except OSError as exc:
            raise HostError(f"cannot clean session {name}: {exc.strerror}") from None
    cleaned = []
    keep = _custom_logs(run_dir)
    for candidate in sorted(_names(run_dir)):
        try:
            if _clean(run_dir, candidate, keep, must=False):
                cleaned.append(candidate)
        except (OSError, HostError):
            continue  # One bad entry does not stop the sweep.
    return cleaned


def _names(run_dir: Path) -> set[str]:
    found = set()
    for entry in os.listdir(run_dir):
        for suffix in (".lock", ".json", ".exit"):
            if entry.endswith(suffix) and paths.NAME_RE.fullmatch(entry[: -len(suffix)]):
                found.add(entry[: -len(suffix)])
    return {n for n in found if _proof(run_dir, n) != (False, False, False)}


def _proof(run_dir: Path, name: str) -> tuple[bool, bool, bool]:
    """Whether the lock, meta, and exit record of name are files that burner made."""
    return (
        _is_lock(run_dir / f"{name}.lock"),
        _is_meta(paths.read_json(run_dir / f"{name}.json")),
        _is_exit(paths.read_json(run_dir / f"{name}.exit")),
    )


def _is_lock(path: Path) -> bool:
    """burner creates lock files empty and never writes to them."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_size == 0


def _is_meta(record: dict | None) -> bool:
    return record is not None and all(k in record for k in ("protocol", "socket", "pid"))


def _is_exit(record: dict | None) -> bool:
    return record is not None and "time" in record and ("code" in record or "signal" in record)


def _custom_logs(run_dir: Path) -> set[str]:
    """The logs that sessions in run_dir chose with log=, which clean never removes."""
    logs = set()
    for entry in os.listdir(run_dir):
        if entry.endswith((".json", ".exit")):
            record = paths.read_json(run_dir / entry) or {}
            log = record.get("log")
            if isinstance(log, str) and log != str(run_dir / (entry.rsplit(".", 1)[0] + ".log")):
                logs.update((log, log + ".1"))
    return logs


def _clean(run_dir: Path, name: str, keep: set[str], must: bool) -> bool:
    lock = run_dir / f"{name}.lock"
    if os.path.lexists(lock) and not _is_lock(lock):
        if must:
            raise HostError(f"cannot clean session {name}: {name}.lock is not a lock file")
        return False
    if _proof(run_dir, name) == (False, False, False):
        return False
    try:
        fd = paths.lock_file(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if must:
            raise HostError(f"session {name} is running; stop it first") from None
        return False
    try:
        # Read the records again: they may have changed before the lock was taken.
        _, has_meta, has_exit = _proof(run_dir, name)
        meta = paths.read_json(run_dir / f"{name}.json") if has_meta else None
        if meta and isinstance(meta.get("socket"), str):
            paths.remove_socket(meta["socket"], run_dir)
        if has_meta:
            _remove(run_dir / f"{name}.json")
        if has_exit:
            _remove(run_dir / f"{name}.exit")
        # An empty lock file alone does not show that the logs are burner's.
        for suffix in OUTPUT_SUFFIXES if has_meta or has_exit else ():
            if str(run_dir / f"{name}{suffix}") not in keep:
                _remove(run_dir / f"{name}{suffix}")
        for entry in os.listdir(run_dir):
            temp = TEMP.fullmatch(entry)
            if temp and temp["name"] == name:
                _remove(run_dir / entry)
        paths.unlink(lock)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return True


def _remove(path: Path) -> bool:
    """Remove a regular file or socket of this user. True when it was removed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISSOCK(st.st_mode)) or st.st_uid != os.geteuid():
        return False
    paths.unlink(path)
    return True
