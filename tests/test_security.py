from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from conftest import PY

from burner import Host, paths
from burner.errors import HostError, UnsafeRunDirError

SLEEP = [PY, "-c", "import time; time.sleep(60)"]


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_private_run_dir_passes(run_dir: Path) -> None:
    paths.check_run_dir(run_dir)
    paths.check_run_dir(run_dir / "missing")


def test_run_dir_writable_by_others_is_refused(run_dir: Path) -> None:
    os.chmod(run_dir, 0o777)
    with pytest.raises(UnsafeRunDirError, match=f"chmod go-w {run_dir}"):
        paths.check_run_dir(run_dir)
    with pytest.raises(UnsafeRunDirError, match="chmod"):
        Host(run_dir).alive("web")
    with pytest.raises(HostError, match="chmod"):
        Host(run_dir).gone("web")
    for mode in (0o777, 0o770):  # Writable by others, and by the group alone.
        os.chmod(run_dir, mode)
        with pytest.raises(UnsafeRunDirError, match="chmod"):
            Host(run_dir).start("web", ["sleep", "60"])
        assert os.listdir(run_dir) == []
    os.chmod(run_dir, 0o700)


def test_run_dir_owned_by_another_user_is_refused() -> None:
    if os.geteuid() == 0:
        pytest.skip("root owns /usr")
    with pytest.raises(UnsafeRunDirError, match="not owned by this user"):
        paths.check_run_dir(Path("/usr"))


def test_run_dir_that_is_a_file_is_refused(run_dir: Path) -> None:
    (run_dir / "file").write_text("")
    with pytest.raises(UnsafeRunDirError, match="not a directory"):
        paths.check_run_dir(run_dir / "file")


def test_parent_writable_by_others_without_sticky_bit_is_refused(run_dir: Path) -> None:
    parent = run_dir / "shared"
    parent.mkdir()
    os.chmod(parent, 0o777)
    (parent / "runs").mkdir(mode=0o700)
    with pytest.raises(UnsafeRunDirError, match=f"{parent}"):
        paths.check_run_dir(parent / "runs")
    os.chmod(parent, 0o1777)
    paths.check_run_dir(parent / "runs")


def test_symlink_to_a_private_dir_passes(run_dir: Path) -> None:
    (run_dir / "real").mkdir(mode=0o700)
    (run_dir / "link").symlink_to(run_dir / "real")
    paths.check_run_dir(run_dir / "link")


def test_run_dir_and_missing_parents_are_made_private(run_dir: Path) -> None:
    old = os.umask(0o022)
    try:
        paths.make_run_dir(run_dir / "a" / "b")
    finally:
        os.umask(old)
    assert mode(run_dir / "a") == mode(run_dir / "a" / "b") == 0o700


def plant_meta(run_dir: Path, name: str, **fields) -> None:
    meta = {"pid": 2, "child": 2, "pgid": 2, "socket": str(run_dir / f"{name}.sock")}
    (run_dir / f"{name}.json").write_text(json.dumps({**meta, **fields}))


def test_start_unlinks_only_its_own_old_socket(host: Host, run_dir: Path) -> None:
    victim = run_dir / "victim"
    victim.write_text("keep")
    plant_meta(run_dir, "a", socket=str(victim))
    host.start("a", SLEEP, cwd=run_dir)
    outside = Path(tempfile.mkdtemp(prefix="sh-out-", dir="/tmp"))
    try:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(outside / "s.sock"))
        plant_meta(run_dir, "b", socket=str(outside / "s.sock"))
        host.start("b", SLEEP, cwd=run_dir)
        assert (outside / "s.sock").exists()
        listener.close()
    finally:
        shutil.rmtree(outside)
    assert victim.read_text() == "keep"
    old = run_dir / "old.sock"
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(old))
    plant_meta(run_dir, "c", socket=str(old))
    host.start("c", SLEEP, cwd=run_dir)
    assert not old.exists()


def test_read_json_takes_only_a_regular_file_of_this_user(run_dir: Path) -> None:
    (run_dir / "real.json").write_text('{"a": 1}')
    assert paths.read_json(run_dir / "real.json") == {"a": 1}
    (run_dir / "link.json").symlink_to(run_dir / "real.json")
    assert paths.read_json(run_dir / "link.json") is None
    os.mkfifo(run_dir / "fifo.json")
    assert paths.read_json(run_dir / "fifo.json") is None


@pytest.mark.parametrize(
    "fields",
    [{"pid": "7"}, {"child": 1}, {"pgid": 0}, {"pgid": -5}, {"pgid": True}, {"socket": 3},
     {"pid": 2.5}],
)
def test_meta_with_bad_fields_counts_as_missing(host: Host, run_dir: Path, fields) -> None:
    plant_meta(run_dir, "m", **fields)
    assert host._meta("m") is None
    plant_meta(run_dir, "m")
    assert host._meta("m") is not None


def test_write_private_does_not_follow_a_planted_temp_file(run_dir: Path) -> None:
    target = run_dir / "target"
    target.write_text("keep")
    (run_dir / f".out.json.{os.getpid()}.tmp").symlink_to(target)
    paths.write_private(run_dir / "out.json", {"a": 1})
    assert target.read_text() == "keep"
    assert paths.read_json(run_dir / "out.json") == {"a": 1}
    assert mode(run_dir / "out.json") == 0o600


def test_write_private_removes_its_temp_file_on_failure(run_dir: Path) -> None:
    with pytest.raises(TypeError):
        paths.write_private(run_dir / "out.json", {"a": object()})
    assert list(run_dir.iterdir()) == []


@pytest.mark.parametrize("custom", [False, True])
def test_log_must_be_a_regular_file_of_this_user(host: Host, run_dir: Path, custom) -> None:
    log = run_dir / ("custom.log" if custom else "w.log")
    kwargs = {"log": log} if custom else {}
    target = run_dir / "target"
    target.write_text("keep")
    log.symlink_to(target)
    with pytest.raises(HostError, match="regular file"):
        host.start("w", SLEEP, cwd=run_dir, **kwargs)
    log.unlink()
    os.mkfifo(log)
    with pytest.raises(HostError, match="regular file"):
        host.start("w", SLEEP, cwd=run_dir, **kwargs)
    assert target.read_text() == "keep"


def test_existing_log_keeps_its_mode(host: Host, run_dir: Path) -> None:
    log = run_dir / "w.log"
    log.write_text("")
    os.chmod(log, 0o640)
    host.start("w", SLEEP, cwd=run_dir)
    assert mode(log) == 0o640
    host.start("n", SLEEP, cwd=run_dir)
    assert mode(run_dir / "n.log") == 0o600


def test_a_relative_run_dir_is_checked(run_dir: Path, monkeypatch) -> None:
    (run_dir / "runs").mkdir(mode=0o700)
    with monkeypatch.context() as m:
        m.chdir(run_dir)
        paths.check_run_dir(Path("runs"))


def test_a_parent_that_cannot_be_read_is_an_unsafe_run_dir(run_dir: Path, monkeypatch) -> None:
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path) == str(run_dir):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *args, **kwargs)

    (run_dir / "runs").mkdir(mode=0o700)
    with monkeypatch.context() as m, pytest.raises(UnsafeRunDirError, match="Permission denied"):
        m.setattr(os, "stat", stat)
        paths.check_run_dir(run_dir / "runs")


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_a_lock_that_is_not_a_regular_file_is_not_trusted(run_dir: Path, kind: str) -> None:
    lock = run_dir / "x.lock"
    os.mkfifo(lock) if kind == "fifo" else lock.mkdir()
    assert paths.lock_probe(lock) is None


@pytest.mark.parametrize("started", [1e300, float("inf"), "now", None])
def test_sessions_skips_a_meta_that_ls_cannot_show(run_dir: Path, started) -> None:
    fd = os.open(run_dir / "odd.lock", os.O_RDONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        meta = {"pid": 2, "child": 2, "pgid": 2, "terminal": False, "started": started,
                "socket": str(run_dir / "odd.sock")}
        (run_dir / "odd.json").write_text(json.dumps(meta))
        assert Host(run_dir).sessions() == []
        ls = subprocess.run([PY, "-m", "burner", "--run-dir", str(run_dir), "ls"],
                            capture_output=True, text=True, timeout=20)
        assert ls.returncode == 0 and ls.stdout == ""
    finally:
        os.close(fd)
        for name in ("odd.lock", "odd.json"):
            (run_dir / name).unlink()

