from __future__ import annotations

import contextlib
import errno
import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import PY, pid_alive, wait_for

from burner import Host, protocol
from burner import host as host_module


def run_patched_host(run_dir: Path, name: str, patch: str, argv: list[str]) -> dict:
    """Run the host with patch applied to burner.host first, and return its report."""
    script = f"import sys\nfrom burner import host\n{patch}\nsys.exit(host.main(sys.argv[1:]))\n"
    request = {"argv": argv, "cwd": "/", "env": {}, "terminal": False, "cols": 80,
               "rows": 24, "log": None}
    proc = subprocess.run(
        [PY, "-c", script, str(run_dir), name],
        input=protocol.frame(protocol.JSON, json.dumps(request).encode()),
        capture_output=True, timeout=20,
    )
    return json.loads(proc.stdout.decode().splitlines()[0])


SLEEPER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)"


def test_a_failure_after_the_spawn_ends_the_child(run_dir: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    patch = (
        "def broken(pid):\n"
        "    import time\n"
        "    time.sleep(0.5)\n"
        "    raise OSError(28, 'no room to watch the child')\n"
        "host.ExitWatch = broken"
    )
    reply = run_patched_host(run_dir, "w", patch, [PY, "-c", SLEEPER, str(pid_file)])
    assert reply["ok"] is False
    child = int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))
    wait_for(lambda: not pid_alive(child))
    assert list(run_dir.iterdir()) == []


# The child leaves a member in its group and exits at once.
BACKGROUND = "import subprocess, sys\nsubprocess.Popen(['sleep', '60'])\n"


def test_an_unknown_exit_status_still_finishes_the_group(host: Host, run_dir: Path) -> None:
    # A child that exits before the host watches it has a status only a reap tells.
    patch = (
        "check = host.ExitWatch.check\n"
        "def unknown(self):\n"
        "    exited = check(self)\n"
        "    self.status = None\n"
        "    return exited\n"
        "host.ExitWatch.check = unknown"
    )
    reply = run_patched_host(run_dir, "bg", patch, [PY, "-c", BACKGROUND])
    assert reply["ok"]
    wait_for(lambda: host.gone("bg"))
    assert host.exit_status("bg").code == 0
    out = subprocess.run(["pgrep", "-g", str(reply["child"])], capture_output=True, text=True)
    assert out.stdout.strip() == ""


def test_a_partly_written_log_counts_what_reached_the_file(
    run_dir: Path, monkeypatch
) -> None:
    h = host_module.SessionHost(run_dir, "w", {"terminal": False}, 0o022)
    h.log = run_dir / "w.log"
    h.log_fd = h._open_log()
    r, w = os.pipe()
    h.log_pipe = r
    h.sel.register(r, 1, h._on_log_pipe)

    def half(fd: int, data: bytes) -> None:
        os.write(fd, data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(host_module, "_write_all", half)
    os.write(w, b"x" * 1000)
    h._read_log_pipe()
    assert h.log_size == (run_dir / "w.log").stat().st_size == 500
    for fd in (r, w, h.log_fd):
        os.close(fd)


def test_a_failed_socket_dir_leaves_no_stale_fd(tmp_path: Path, monkeypatch) -> None:
    h = host_module.SessionHost(tmp_path, "s", {"terminal": False}, 0o022)
    h._make_socket_dir()
    first = h.sock_dir
    try:
        def full(**kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(host_module.tempfile, "mkdtemp", full)
        for _ in range(2):  # A later try fails the same way, not on a closed fd.
            try:
                h._make_socket_dir()
            except OSError as exc:
                assert exc.errno == errno.ENOSPC
        assert h.sock_dir_fd == -1
    finally:
        os.rmdir(first)


class _Ended:
    pid, returncode = 999_999, 0

    def wait(self, timeout=None) -> int:
        return 0


class _Watch:
    exited, status = True, {"code": 0}

    def check(self) -> bool:
        return True

    def close(self) -> None:
        pass


def test_abort_after_the_end_leaves_a_new_host_files_alone(run_dir: Path) -> None:
    h = host_module.SessionHost(run_dir, "s", {"terminal": False}, 0o022)
    h.lock_fd = os.open(run_dir / "old.lock", os.O_RDONLY | os.O_CREAT, 0o600)
    h.child, h.exit_watch = _Ended(), _Watch()
    h.lingering = h.reported = h.stopping = True
    # The name is free again, and a new host has written its meta.
    (run_dir / "s.json").write_text("{}")
    try:
        h.abort("the loop broke")
        assert (run_dir / "s.json").exists()
    finally:
        os.close(h.lock_fd)
        (run_dir / "old.lock").unlink()
        (run_dir / "s.json").unlink()


def test_a_socket_dir_that_cannot_be_locked_is_removed(tmp_path: Path, monkeypatch) -> None:
    h = host_module.SessionHost(tmp_path, "s", {"terminal": False}, 0o022)
    made = []
    real = host_module.tempfile.mkdtemp

    def record(**kwargs):
        made.append(real(**kwargs))
        return made[-1]

    def no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(host_module.tempfile, "mkdtemp", record)
    monkeypatch.setattr(host_module.fcntl, "flock", no_locks)
    try:
        with pytest.raises(OSError, match="No locks available"):
            h._make_socket_dir()
        assert made and not os.path.exists(made[0])
        assert h.sock_dir is None and h.sock_dir_fd == -1
    finally:
        for path in made:
            with contextlib.suppress(OSError):
                os.rmdir(path)
