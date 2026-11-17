from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
from pathlib import Path

import pytest
from conftest import PY, pid_alive, wait_for

from burner import Host, HostError, cleanup

SLEEP = [PY, "-c", "import time; time.sleep(60)"]
READY = [PY, "-c", "import time; print('ready', flush=True); time.sleep(60)"]


@pytest.fixture(params=["short", "long"])
def where(request) -> Path:
    return request.getfixturevalue("run_dir" if request.param == "short" else "long_run_dir")


@pytest.fixture(params=[False, True], ids=["log", "terminal"])
def terminal(request) -> bool:
    return request.param


def files(run_dir: Path) -> set[str]:
    return {p.name for p in run_dir.iterdir()} if run_dir.exists() else set()


def ended(terminal: bool) -> set[str]:
    return {"s.exit"} if terminal else {"s.exit", "s.log"}


def start(host: Host, terminal: bool, argv: list[str] = READY) -> dict:
    host.start("s", argv, cwd="/", terminal=terminal)
    wait_for(lambda: "ready" in (host.screen("s") if terminal else host.tail("s")))
    return json.loads((host.run_dir / "s.json").read_text())


@pytest.mark.parametrize("end", ["exit", "stop", "sigterm"])
def test_row_normal_end(where: Path, terminal: bool, end: str) -> None:
    host = Host(where, timeout=5)
    argv = [PY, "-c", "import time; print('ready', flush=True); time.sleep(0.5)"]
    meta = start(host, terminal, argv if end == "exit" else READY)
    if end == "stop":
        host.stop("s")
    elif end == "sigterm":
        os.kill(meta["pid"], signal.SIGTERM)
    wait_for(lambda: host.gone("s"))
    assert files(where) == ended(terminal)
    assert not Path(meta["socket"]).exists()
    assert host.clean() == ["s"]
    assert files(where) == set()


def test_row_failed_start(where: Path, terminal: bool) -> None:
    host = Host(where, timeout=5)
    host.start("s", [PY, "-c", "print('ready')"], cwd="/", terminal=terminal)
    wait_for(lambda: host.gone("s"))
    before = {p: (where / p).read_bytes() for p in files(where)}
    with pytest.raises(HostError):
        host.start("s", ["no-such-program-xyz"], cwd="/", terminal=terminal)
    assert {p: (where / p).read_bytes() for p in files(where)} == before


def test_row_host_killed(where: Path, terminal: bool) -> None:
    host = Host(where, timeout=5)
    meta = start(host, terminal)
    os.kill(meta["pid"], signal.SIGKILL)
    wait_for(lambda: not pid_alive(meta["pid"]))
    left = {"s.lock", "s.json"} | ({"s.sock"} if meta["socket"].startswith(str(where)) else set())
    assert left | ({"s.log"} if not terminal else set()) <= files(where)
    if not terminal:
        assert host.alive("s")
        assert host.clean() == []
        host.stop("s", grace=1.0)
    wait_for(lambda: host.gone("s"))
    wait_for(lambda: not pid_alive(meta["child"]))  # The reap follows the freed name.
    assert host.clean() == ["s"]
    assert files(where) == set()
    assert not Path(meta["socket"]).parent.exists() or meta["socket"].startswith(str(where))


def test_row_run_dir_deleted(where: Path, terminal: bool) -> None:
    host = Host(where, timeout=5)
    meta = start(host, terminal)
    shutil.rmtree(where)
    wait_for(lambda: not pid_alive(meta["child"]) and not pid_alive(meta["pid"]), timeout=15)
    assert files(where) == set()
    assert not Path(meta["socket"]).exists()
    if not meta["socket"].startswith(str(where)):
        assert not Path(meta["socket"]).parent.exists()


def test_row_reboot(where: Path, terminal: bool) -> None:
    host = Host(where, timeout=5)
    meta = start(host, terminal)
    host.stop("s")
    # After a reboot nothing holds the lock, and the pids in the meta name nothing.
    reboot = {**meta, "pid": 999_999, "child": 999_998, "pgid": 999_998, "sid": 999_997}
    (where / "s.json").write_text(json.dumps(reboot))
    (where / "s.lock").touch()
    assert host.gone("s")
    assert host.clean("s") == ["s"]
    assert files(where) == set()


def test_clean_skips_a_live_session_and_refuses_it_by_name(host: Host, run_dir: Path) -> None:
    host.start("live", SLEEP, cwd="/")
    host.start("done", [PY, "-c", "pass"], cwd="/")
    wait_for(lambda: host.gone("done"))
    assert host.clean() == ["done"]
    assert {"live.lock", "live.json", "live.sock", "live.log"} <= files(run_dir)
    with pytest.raises(HostError, match="live"):
        host.clean("live")
    assert host.alive("live")


def meta_record(**fields) -> str:
    return json.dumps({"protocol": 1, "socket": "/nowhere/x.sock", "pid": 2, **fields})


def exit_record(**fields) -> str:
    return json.dumps({"time": 1.0, "code": 0, **fields})


def test_clean_removes_only_checked_files_of_the_name(host: Host, run_dir: Path) -> None:
    target = run_dir / "target"
    target.write_text("keep")
    (run_dir / "a.log").symlink_to(target)
    (run_dir / "a.exit").mkdir()
    (run_dir / "a.log.1").write_text("old")
    (run_dir / f".a.json.{os.getpid()}.tmp").write_text("{}")
    (run_dir / ".a.b.json.7.tmp").write_text("{}")
    (run_dir / "a.b.exit").write_text("{}")
    custom = run_dir / "custom.log"
    custom.write_text("mine")
    (run_dir / "a.json").write_text(meta_record(log=str(custom)))
    assert host.clean("a") == ["a"]
    assert files(run_dir) == {"target", "a.log", "a.exit", ".a.b.json.7.tmp", "a.b.exit",
                              "custom.log"}
    assert target.read_text() == "keep" and custom.read_text() == "mine"
    (run_dir / "b.exit").write_text(exit_record(log=str(custom)))
    (run_dir / "lonely.log").write_text("no session names this")
    assert host.clean() == ["b"]
    assert files(run_dir) == {"target", "a.log", "a.exit", ".a.b.json.7.tmp", "a.b.exit",
                              "custom.log", "lonely.log"}
    assert run_dir.is_dir()


def test_clean_leaves_files_burner_did_not_make(host: Host, run_dir: Path) -> None:
    foreign = {
        "package.json": '{"name": "app"}', "yarn.lock": "# yarn lockfile v1\n",
        "tsconfig.json": "{}", "Cargo.lock": "version = 3\n", "data.exit": "[1, 2]",
        "notes.log": "notes\n",
    }
    for name, text in foreign.items():
        (run_dir / name).write_text(text)
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(run_dir / "app.sock"))
    assert host.clean() == []
    for name in ("package", "tsconfig", "data", "notes", "app"):
        assert host.clean(name) == []
    for name in ("yarn", "Cargo"):
        with pytest.raises(HostError, match=f"{name}.lock is not a lock file"):
            host.clean(name)
    assert files(run_dir) == {*foreign, "app.sock"}


def test_clean_skips_a_lock_that_is_not_a_file(host: Host, run_dir: Path) -> None:
    (run_dir / "target").write_text("")
    (run_dir / "x.lock").symlink_to(run_dir / "target")
    (run_dir / "x.exit").write_text(exit_record())
    (run_dir / "y.lock").mkdir()
    (run_dir / "y.exit").write_text(exit_record())
    (run_dir / "z.exit").write_text(exit_record())
    assert host.clean() == ["z"]
    for name in ("x", "y"):
        with pytest.raises(HostError, match=f"cannot clean session {name}"):
            host.clean(name)
    assert files(run_dir) == {"target", "x.lock", "x.exit", "y.lock", "y.exit"}


def test_clean_of_a_missing_run_dir(run_dir: Path) -> None:
    assert cleanup.clean(run_dir / "missing") == []
    assert not (run_dir / "missing").exists()


def test_cli_clean(host: Host, run_dir: Path) -> None:
    host.start("live", SLEEP, cwd="/")
    host.start("done", [PY, "-c", "pass"], cwd="/")
    wait_for(lambda: host.gone("done"))

    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([PY, "-m", "burner.cli", "--run-dir", str(run_dir), *args],
                              capture_output=True, text=True, timeout=20)

    result = cli("clean", "live")
    assert result.returncode == 1 and result.stderr.count("\n") == 1
    assert cli("clean").stdout == "done\n"
    assert cli("clean", "done").stdout == ""


def test_clean_keeps_the_logs_of_a_name_that_only_an_empty_lock_proves(
    host: Host, run_dir: Path
) -> None:
    (run_dir / "server.lock").write_text("")
    (run_dir / "server.log").write_text("another tool's log\n")
    (run_dir / "server.log.1").write_text("older\n")
    assert host.clean() == ["server"]
    assert files(run_dir) == {"server.log", "server.log.1"}
