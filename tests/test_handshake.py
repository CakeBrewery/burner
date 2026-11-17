from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from conftest import PY, wait_for

from burner import Host, protocol


def ps() -> str:
    return subprocess.run(["ps", "axww", "-o", "pid=,args="], capture_output=True,
                          text=True).stdout


def test_a_host_whose_starter_left_undoes_the_start(run_dir: Path) -> None:
    mark = f"handshake-{run_dir.name}"
    request = {"argv": [PY, "-c", "import time; time.sleep(60)", mark], "cwd": "/",
               "env": {}, "terminal": False, "cols": 80, "rows": 24, "log": None}
    proc = subprocess.Popen([PY, "-m", "burner.host", str(run_dir), "gone"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proc.stdout.close()  # Nobody reads the ready report.
    proc.stdin.write(protocol.frame(protocol.JSON, json.dumps(request).encode()))
    proc.stdin.close()
    proc.wait(5)
    wait_for(lambda: f"burner.host {run_dir} " not in ps() and mark not in ps())
    assert list(run_dir.iterdir()) == []


def test_the_host_runs_with_a_safe_path(host: Host, run_dir: Path) -> None:
    host.start("p", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    assert f"-P -m burner.host {run_dir} p" in ps()


def test_wrong_arguments_print_one_usage_line(run_dir: Path) -> None:
    for args in ([], [str(run_dir)], [str(run_dir), "bad name"], [str(run_dir), "a", "b"]):
        started = time.monotonic()
        result = subprocess.run([PY, "-m", "burner.host", *args], capture_output=True,
                                text=True, timeout=10, stdin=subprocess.DEVNULL)
        assert result.returncode == 2
        assert result.stderr.startswith("usage: ") and result.stderr.count("\n") == 1
        assert result.stdout == ""
        assert time.monotonic() - started < 5
    assert list(run_dir.iterdir()) == []
