from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from conftest import PY, pid_alive, wait_for

from burner import Host
from burner.host import ExitWatch

# Notes each signal it gets in a file, and exits on the one named in argv[2].
RECORDER = r"""
import os, signal, sys, time
def note(sig, _):
    with open(sys.argv[1], "a") as f:
        f.write(signal.Signals(sig).name + "\n")
    if sig == getattr(signal, sys.argv[2]):
        sys.exit(0)
for sig in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(sig, note)
print("ready", flush=True)
while True:
    time.sleep(1)
"""


def start_recorder(host: Host, run_dir: Path, terminal: bool) -> Path:
    notes = run_dir / "notes"
    host.start("rec", [PY, "-c", RECORDER, str(notes), "SIGTERM"], cwd=run_dir,
               terminal=terminal)
    if terminal:
        wait_for(lambda: "ready" in host.screen("rec"))
    else:
        wait_for(lambda: "ready" in host.tail("rec"))
    return notes


@pytest.mark.parametrize(("terminal", "order"), [(False, "SIGTERM\n"),
                                                 (True, "SIGHUP\nSIGTERM\n")])
def test_stop_order(host: Host, run_dir: Path, terminal: bool, order: str) -> None:
    notes = start_recorder(host, run_dir, terminal)
    host.stop("rec", grace=0.4)
    assert notes.read_text() == order


@pytest.mark.parametrize(("terminal", "order"), [(False, "SIGTERM\n"),
                                                 (True, "SIGHUP\nSIGTERM\n")])
def test_stop_order_from_the_client(host: Host, run_dir: Path, terminal: bool, order: str):
    notes = start_recorder(host, run_dir, terminal)
    pid = json.loads((run_dir / "rec.json").read_text())["pid"]
    os.kill(pid, signal.SIGSTOP)
    try:
        host.stop("rec", grace=0.4)
    finally:
        if pid_alive(pid):
            os.kill(pid, signal.SIGCONT)
    assert notes.read_text() == order
    host.clean("rec")


# The leader exits on the first signal. Its group member ignores polite signals.
GROUP = r"""
import os, signal, subprocess, sys, time
code = (
    "import signal, time\n"
    "for s in (signal.SIGHUP, signal.SIGTERM):\n"
    "    signal.signal(s, signal.SIG_IGN)\n"
    "time.sleep(60)\n"
)
p = subprocess.Popen([sys.executable, "-c", code])
open(sys.argv[1], "w").write(str(p.pid))
time.sleep(float(sys.argv[2]))
"""


@pytest.mark.parametrize("terminal", [False, True])
def test_stop_finishes_the_group_and_frees_the_name_at_once(
    host: Host, run_dir: Path, terminal: bool
) -> None:
    pid_file = run_dir / "member.pid"
    host.start("grp", [PY, "-c", GROUP, str(pid_file), "60"], cwd=run_dir, terminal=terminal)
    member = int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))
    try:
        time.sleep(0.3)  # The member sets its handlers.
        started = time.monotonic()
        host.stop("grp", grace=1.0)
        assert host.gone("grp") and time.monotonic() - started < 1.0
        assert pid_alive(member)
        wait_for(lambda: not pid_alive(member), timeout=2.0)
        assert time.monotonic() - started < 2.0
    finally:
        kill(member)


def kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def host_pids(run_dir: Path) -> list[str]:
    out = subprocess.run(["ps", "axww", "-o", "pid=,args="], capture_output=True,
                         text=True).stdout
    return [line for line in out.splitlines() if f"burner.host {run_dir} " in line]


@pytest.mark.parametrize("terminal", [False, True])
def test_a_member_outlives_the_leader_by_at_most_grace(
    host: Host, run_dir: Path, terminal: bool
) -> None:
    pid_file = run_dir / "member.pid"
    host.start("grp", [PY, "-c", GROUP, str(pid_file), "0.5"], cwd=run_dir, terminal=terminal)
    member = int(wait_for(lambda: pid_file.exists() and pid_file.read_text()))
    try:
        wait_for(lambda: host.gone("grp"))
        ended = time.monotonic()
        assert host.exit_status("grp").code == 0
        assert host_pids(run_dir) and pid_alive(member)
        wait_for(lambda: not pid_alive(member), timeout=7)
        assert time.monotonic() - ended > 3
        wait_for(lambda: not host_pids(run_dir), timeout=2)
    finally:
        kill(member)


@pytest.mark.parametrize("terminal", [False, True])
def test_the_host_exits_soon_after_a_clean_exit(host: Host, run_dir: Path, terminal) -> None:
    host.start("bye", [PY, "-c", "import time; time.sleep(0.3)"], cwd=run_dir,
               terminal=terminal)
    wait_for(lambda: host.gone("bye"))
    ended = time.monotonic()
    wait_for(lambda: not host_pids(run_dir), timeout=2, interval=0.02)
    assert time.monotonic() - ended < 1.0


@pytest.mark.parametrize(("argv", "status"), [
    (["sh", "-c", "read go; exit 3"], {"code": 3}),
    (["sh", "-c", "read go; kill -TERM $$"], {"signal": signal.SIGTERM}),
])
def test_exit_watch_tells_how_the_child_ended(argv: list[str], status: dict) -> None:
    # The child exits only once the watch is in place.
    child = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.PIPE)
    watch = ExitWatch(child.pid)
    child.stdin.write(b"\n")
    child.stdin.close()
    try:
        wait_for(watch.check)
        assert child.returncode is None
        assert watch.status == status
    finally:
        watch.close()
        child.wait(5)
