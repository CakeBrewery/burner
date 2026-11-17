from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from conftest import PY, wait_for

from burner import Host, protocol
from burner import host as host_module

LIMIT = host_module.LOG_LIMIT_BYTES


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or 0) / 1024


def log_bytes(run_dir: Path, name: str) -> int:
    total = 0
    for suffix in (".log", ".log.1"):
        with contextlib.suppress(FileNotFoundError):  # The host may be moving it.
            total += (run_dir / f"{name}{suffix}").stat().st_size
    return total


FAST = r"""
import os, sys, time
end = time.monotonic() + float(sys.argv[1])
block = b"x" * 65535 + b"\n"
while time.monotonic() < end:
    os.write(1, block)
"""


def test_a_fast_writer_keeps_the_log_bounded(host: Host, run_dir: Path) -> None:
    host.start("fast", [PY, "-c", FAST, "2"], cwd=run_dir)
    host_pid = json.loads((run_dir / "fast.json").read_text())["pid"]
    peak_disk = peak_rss = 0.0
    while host.alive("fast"):
        peak_disk = max(peak_disk, log_bytes(run_dir, "fast"))
        peak_rss = max(peak_rss, rss_mb(host_pid))
        time.sleep(0.05)
    assert (run_dir / "fast.log.1").exists()
    assert max(peak_disk, log_bytes(run_dir, "fast")) <= 2 * LIMIT + 64 * 1024
    assert peak_rss < 24


WRITE_THEN_WAIT = r"""
import os, sys, time
block = b"y" * 65535 + b"\n"
for _ in range(int(sys.argv[1]) // len(block) + 1):
    os.write(1, block)
print("written", flush=True)
time.sleep(60)
"""


def test_host_errors_land_in_the_current_log(host: Host, run_dir: Path) -> None:
    host.start("rot", [PY, "-c", WRITE_THEN_WAIT, str(2 * LIMIT + 1)], cwd=run_dir)
    wait_for(lambda: host.tail("rot", 1) == "written", timeout=10)
    assert (run_dir / "rot.log.1").stat().st_size <= LIMIT
    (run_dir / "rot.json").unlink()
    os.chmod(run_dir, 0o500)
    try:
        wait_for(lambda: "cannot check the session's files" in host.tail("rot", 1),
                 timeout=15)
    finally:
        os.chmod(run_dir, 0o700)
    wait_for((run_dir / "rot.json").exists, timeout=10)
    assert "cannot check" not in (run_dir / "rot.log.1").read_text()[-200:]


def test_output_at_the_child_exit_is_kept(host: Host, run_dir: Path) -> None:
    script = "import sys; sys.stdout.write('z' * 300000 + '\\nend\\n')"
    host.start("out", [PY, "-c", script], cwd=run_dir)
    wait_for(lambda: host.gone("out"))
    assert host.tail("out", 1) == "end"


def test_a_terminal_session_creates_no_log(host: Host, run_dir: Path) -> None:
    host.start("tui", [PY, "-c", "print('hi')"], cwd=run_dir, terminal=True)
    wait_for(lambda: host.gone("tui"))
    assert not (run_dir / "tui.log").exists()


BROKEN = """
import sys
from burner import host
def broken(self):
    raise RuntimeError("the loop broke")
host.SessionHost.run = broken
sys.exit(host.main(sys.argv[1:]))
"""


@pytest.mark.parametrize("terminal", [False, True])
def test_a_host_error_goes_to_the_exit_record(host: Host, run_dir: Path, terminal) -> None:
    request = {"argv": [PY, "-c", "import time; time.sleep(60)"], "cwd": "/", "env": {},
               "terminal": terminal, "cols": 80, "rows": 24, "log": None}
    proc = subprocess.run(
        [PY, "-c", BROKEN, str(run_dir), "bad"],
        input=protocol.frame(protocol.JSON, json.dumps(request).encode()),
        capture_output=True, timeout=10,
    )
    assert json.loads(proc.stdout.decode().splitlines()[0])["ok"]
    wait_for(lambda: host.gone("bad"))
    assert "the loop broke" in json.loads((run_dir / "bad.exit").read_text())["error"]
    assert (run_dir / "bad.log").exists() != terminal


# The child leaves a member in its group that ignores SIGTERM and SIGPIPE and
# keeps writing to the log pipe after the child exits.
OLD_WRITER = r"""
import os, signal, subprocess, sys, time
code = '''
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGPIPE, signal.SIG_IGN)
open(sys.argv[1], "w").write(str(os.getpid()))
while True:
    try:
        os.write(1, b"OLD\\n")
    except OSError:
        pass
    time.sleep(0.02)
'''
subprocess.Popen([sys.executable, "-c", code, sys.argv[1]])
while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
"""

NEW_WRITER = (
    "import time\nprint('NEW START', flush=True)\n"
    "for _ in range(50):\n    print('NEW', flush=True)\n    time.sleep(0.02)\n"
    "time.sleep(60)"
)


def test_a_lingering_group_does_not_write_into_the_next_log(host: Host, run_dir: Path) -> None:
    pid_file = run_dir / "member.pid"
    host.start("svc", [PY, "-c", OLD_WRITER, str(pid_file)], cwd=run_dir)
    member = int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))
    try:
        wait_for(lambda: host.gone("svc"))
        host.start("svc", [PY, "-c", NEW_WRITER], cwd=run_dir)
        wait_for(lambda: host.tail("svc", 1000).count("NEW\n") >= 49, timeout=10)
        after = host.tail("svc", 1000).split("NEW START\n", 1)[1]
        assert "OLD" not in after
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(member, signal.SIGKILL)
