from __future__ import annotations

import os
import stat
from pathlib import Path

from conftest import PY, wait_for

from burner import Host

COUNT = (
    "import sys\nfor i in range(int(sys.argv[1])): print('line', i)\n"
    "sys.stdout.flush()\nprint('err', file=sys.stderr)"
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_tail_and_persistence(host: Host, run_dir: Path) -> None:
    host.start("count", [PY, "-c", COUNT, "100"], cwd=run_dir)
    wait_for(lambda: host.gone("count"))
    assert host.log_path("count") == run_dir / "count.log"
    assert host.tail("count", 3) == "line 98\nline 99\nerr"
    assert host.tail("count", 0) == ""
    assert len(host.tail("count", 1000).splitlines()) == 101
    assert host.exit_status("count").code == 0


def test_log_appends_across_runs(host: Host, run_dir: Path) -> None:
    for _ in range(2):
        host.start("count", [PY, "-c", COUNT, "1"], cwd=run_dir)
        wait_for(lambda: host.gone("count"))
    assert host.tail("count") == "line 0\nerr\nline 0\nerr"


def test_custom_log_path(host: Host, run_dir: Path) -> None:
    log = run_dir / "sub" / "custom.log"
    log.parent.mkdir()
    host.start("count", [PY, "-c", COUNT, "2"], cwd=run_dir, log=log)
    assert host.log_path("count") == log
    wait_for(lambda: host.gone("count"))
    assert host.log_path("count") == log
    assert host.tail("count") == "line 0\nline 1\nerr"


def test_rotation_over_5_mb(host: Host, run_dir: Path) -> None:
    log = run_dir / "big.log"
    log.write_bytes(b"old\n" * (5 * 1024 * 1024 // 4 + 1))
    (run_dir / "big.log.1").write_text("older\n")
    host.start("big", [PY, "-c", COUNT, "1"], cwd=run_dir)
    wait_for(lambda: host.gone("big"))
    assert (run_dir / "big.log.1").stat().st_size > 5 * 1024 * 1024
    assert host.tail("big") == "line 0\nerr"


def test_tail_of_large_log_reads_the_end(host: Host, run_dir: Path) -> None:
    log = run_dir / "huge.log"
    with open(log, "wb") as f:
        f.write(b"\xff" * 4_000_000 + b"\n")
        f.write(b"".join(b"row %d\n" % i for i in range(1000)))
    assert host.tail("huge", 2) == "row 998\nrow 999"
    assert host.tail("huge", 1001).splitlines()[0] == "�" * 4_000_000


def test_file_modes(host: Host, run_dir: Path) -> None:
    fresh = run_dir / "fresh"
    h = Host(fresh)
    h.start("svc", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    assert mode(fresh) == 0o700
    for suffix in (".log", ".json", ".lock", ".sock"):
        assert mode(fresh / f"svc{suffix}") == 0o600, suffix
    h.stop("svc")
    assert mode(fresh / "svc.exit") == 0o600


def test_log_session_child_has_no_stdin(host: Host, run_dir: Path) -> None:
    script = "import sys; print(repr(sys.stdin.read()))"
    host.start("stdin", [PY, "-c", script], cwd=run_dir)
    wait_for(lambda: host.gone("stdin"))
    assert host.tail("stdin") == "''"


def test_child_umask_is_the_callers(host: Host, run_dir: Path) -> None:
    script = "import os; m = os.umask(0); print(oct(m))"
    host.start("umask", [PY, "-c", script], cwd=run_dir)
    wait_for(lambda: host.gone("umask"))
    current = os.umask(0)
    os.umask(current)
    assert host.tail("umask") == oct(current)
