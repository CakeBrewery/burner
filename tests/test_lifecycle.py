from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import ECHO, PY, STUBBORN, pid_alive, wait_for

from burner import Host, HostError, NotRunningError

SLEEP = [PY, "-c", "import time; time.sleep(60)"]


def meta(run_dir: Path, name: str) -> dict:
    return json.loads((run_dir / f"{name}.json").read_text())


def test_log_session_lifecycle(host: Host, run_dir: Path) -> None:
    assert host.gone("svc") and not host.alive("svc")
    host.start("svc", SLEEP, cwd=run_dir)
    assert host.alive("svc") and not host.gone("svc")
    [session] = host.sessions()
    assert session.name == "svc" and not session.terminal
    assert pid_alive(session.pid)
    info = meta(run_dir, "svc")
    assert info["child"] == info["pgid"] == session.pid
    assert info["protocol"] == 1
    sock = Path(info["socket"])
    assert sock.exists()
    host.stop("svc")
    assert host.gone("svc") and not host.alive("svc")
    assert host.exit_status("svc").signal == signal.SIGTERM
    assert not sock.exists() and not (run_dir / "svc.json").exists()
    assert host.sessions() == []
    host.stop("svc")


def test_terminal_session_lifecycle(host: Host, run_dir: Path) -> None:
    host.start("tui", [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("tui"))
    [session] = host.sessions()
    assert session.terminal
    host.stop("tui")
    assert host.gone("tui")
    wait_for(lambda: not pid_alive(session.pid))  # The reap follows the freed name.


@pytest.mark.parametrize("terminal", [False, True])
def test_gone_after_child_exits(host: Host, run_dir: Path, terminal: bool) -> None:
    host.start("short", [PY, "-c", "print('bye'); raise SystemExit(3)"], cwd=run_dir,
               terminal=terminal)
    wait_for(lambda: host.gone("short"))
    assert host.exit_status("short").code == 3
    with pytest.raises(NotRunningError):
        host.screen("short")


def test_start_removes_old_exit_status(host: Host, run_dir: Path) -> None:
    host.start("again", [PY, "-c", "raise SystemExit(5)"], cwd=run_dir)
    wait_for(lambda: host.gone("again"))
    assert host.exit_status("again").code == 5
    host.start("again", SLEEP, cwd=run_dir)
    assert host.exit_status("again") is None


STARTER = """
import sys, time
from burner import Host
Host(sys.argv[1]).start("kept", [sys.executable, "-c", "import time; time.sleep(60)"], cwd="/",
                        terminal=sys.argv[2] == "1")
print("started", flush=True)
if sys.argv[3] == "wait":
    time.sleep(60)
"""


@pytest.mark.parametrize("terminal", ["0", "1"])
def test_survives_starter_exit(host: Host, run_dir: Path, terminal: str) -> None:
    subprocess.run([PY, "-c", STARTER, str(run_dir), terminal, "exit"], check=True, timeout=10)
    assert host.alive("kept")


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGINT])
def test_survives_signal_to_starter_group(host: Host, run_dir: Path, sig: int) -> None:
    starter = subprocess.Popen([PY, "-c", STARTER, str(run_dir), "1", "wait"],
                               stdout=subprocess.PIPE, start_new_session=True)
    assert starter.stdout.readline() == b"started\n"
    os.killpg(starter.pid, sig)
    starter.wait(5)
    starter.stdout.close()
    assert host.alive("kept")
    assert "kept" in [s.name for s in host.sessions()]


def test_stop_kills_stubborn_child_and_grandchild(host: Host, run_dir: Path) -> None:
    script = textwrap.dedent(f"""
        import signal, subprocess, sys, time
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        p = subprocess.Popen([sys.executable, "-c", {STUBBORN!r}])
        print("grandchild", p.pid, flush=True)
        time.sleep(60)
    """)
    host.start("stub", [PY, "-c", script], cwd=run_dir)
    line = wait_for(lambda: [x for x in host.tail("stub").split("\n") if "grandchild" in x])
    grandchild = int(line[0].split()[1])
    wait_for(lambda: "stubborn" in host.tail("stub"))
    child = host.sessions()[0].pid
    host.stop("stub", grace=1.0)
    assert host.gone("stub")
    assert host.exit_status("stub").signal == signal.SIGKILL
    wait_for(lambda: not pid_alive(grandchild) and not pid_alive(child))


def test_stop_works_on_frozen_host(host: Host, run_dir: Path) -> None:
    host.start("frozen", [PY, "-c", STUBBORN], cwd=run_dir)
    info = meta(run_dir, "frozen")
    os.kill(info["pid"], signal.SIGSTOP)
    try:
        host.stop("frozen", grace=1.0)
    finally:
        if pid_alive(info["pid"]):
            os.kill(info["pid"], signal.SIGCONT)
    assert host.gone("frozen")
    wait_for(lambda: not pid_alive(info["child"]))
    host.clean("frozen")


def test_sigterm_to_host_stops_session(host: Host, run_dir: Path) -> None:
    host.start("term", SLEEP, cwd=run_dir, terminal=True)
    os.kill(meta(run_dir, "term")["pid"], signal.SIGTERM)
    wait_for(lambda: host.gone("term"))


def test_sigkill_on_host_frees_name_and_ends_terminal_child(host: Host, run_dir: Path) -> None:
    host.start("crash", [PY, "-c", ECHO], cwd=run_dir, terminal=True)
    wait_for(lambda: "ready" in host.screen("crash"))
    info = meta(run_dir, "crash")
    os.kill(info["pid"], signal.SIGKILL)
    wait_for(lambda: host.gone("crash"))
    wait_for(lambda: not pid_alive(info["child"]))
    assert (run_dir / "crash.json").exists()
    assert host.sessions() == []
    with pytest.raises(NotRunningError):
        host.screen("crash")
    host.start("crash", SLEEP, cwd=run_dir)
    assert meta(run_dir, "crash")["pid"] != info["pid"]


ENV_DUMP = "import json, os, time; print(json.dumps(dict(os.environ)), flush=True); time.sleep(60)"


def test_environment_complete_and_secret_hidden(host: Host, run_dir: Path) -> None:
    secret = "s3cr3t-value-9f2e"
    env = {"SECRET_TOKEN": secret, "PATH": os.environ["PATH"], "EMPTY": ""}
    host.start("dump", ["/usr/bin/env"], cwd=run_dir, env=env)
    wait_for(lambda: host.gone("dump"))
    seen = dict(line.split("=", 1) for line in host.tail("dump").splitlines())
    seen.pop("__CF_USER_TEXT_ENCODING", None)
    assert seen == env
    host.start("env", SLEEP, cwd=run_dir, env=env, terminal=True)
    info = meta(run_dir, "env")
    pids = f"{info['pid']},{info['child']}"
    command = ["ps", "-ww", "-o", "args=", "-p", pids]
    ps = subprocess.run(command, capture_output=True, text=True).stdout
    lines = ps.strip().splitlines()
    assert len(lines) == 2
    assert secret not in ps
    assert any(x.rstrip().endswith(f"-m burner.host {run_dir} env") for x in lines)


def test_caller_environment_by_default(host: Host, run_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("BURNER_TEST_MARK", "inherited")
    host.start("env", [PY, "-c", ENV_DUMP], cwd=run_dir)
    seen = json.loads(wait_for(lambda: host.tail("env", 1)))
    assert seen["BURNER_TEST_MARK"] == "inherited"


def test_large_argument(host: Host, run_dir: Path) -> None:
    big = "p" * 100_000
    host.start("big", [PY, "-c", "import sys; print(len(sys.argv[1]))", big], cwd=run_dir)
    wait_for(lambda: host.gone("big"))
    assert host.tail("big") == "100000"


LISTENER = """
import socket, sys
from burner import Host
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen()
s.set_inheritable(True)
Host(sys.argv[1]).start("fds", [sys.executable, "-c", "import time; time.sleep(60)"], cwd="/")
print(s.getsockname()[1])
"""


def test_no_inherited_fds(host: Host, run_dir: Path) -> None:
    out = subprocess.run([PY, "-c", LISTENER, str(run_dir)], capture_output=True, text=True,
                         check=True, timeout=10).stdout
    assert host.alive("fds")
    port = int(out)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_host_does_not_hold_starter_cwd(host: Host, run_dir: Path) -> None:
    host.start("cwd", SLEEP, cwd=run_dir)
    pid = meta(run_dir, "cwd")["pid"]
    if Path("/proc").is_dir():
        assert os.readlink(f"/proc/{pid}/cwd") == "/"
    else:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
                             capture_output=True, text=True).stdout
        assert "n/\n" in out


def test_child_runs_in_cwd(host: Host, run_dir: Path) -> None:
    host.start("pwd", [PY, "-c", "import os; print(os.getcwd())"], cwd=run_dir)
    wait_for(lambda: host.gone("pwd"))
    assert Path(host.tail("pwd")).resolve() == run_dir.resolve()


def test_double_start_refused(host: Host, run_dir: Path) -> None:
    host.start("one", SLEEP, cwd=run_dir)
    with pytest.raises(HostError, match="already running"):
        host.start("one", SLEEP, cwd=run_dir)
    assert host.alive("one")


def test_missing_binary(host: Host, run_dir: Path) -> None:
    with pytest.raises(HostError, match="no-such-program-xyz: No such file"):
        host.start("bad", ["no-such-program-xyz"], cwd=run_dir)
    assert host.gone("bad")


def test_bad_cwd(host: Host, run_dir: Path) -> None:
    with pytest.raises(HostError, match="missing-dir"):
        host.start("bad", SLEEP, cwd=run_dir / "missing-dir")
    assert host.gone("bad")


@pytest.mark.parametrize("name", ["", "-x", ".x", "a/b", "a b", "x" * 101, "é"])
def test_name_validation(host: Host, run_dir: Path, name: str) -> None:
    with pytest.raises(ValueError):
        host.start(name, SLEEP, cwd=run_dir)
    with pytest.raises(ValueError):
        host.alive(name)


def test_longest_name(host: Host, run_dir: Path) -> None:
    name = "n" * 100
    host.start(name, SLEEP, cwd=run_dir)
    assert host.alive(name)


def test_run_dirs_isolated(host: Host, run_dir: Path, long_run_dir: Path) -> None:
    other = Host(long_run_dir)
    host.start("same", SLEEP, cwd=run_dir)
    assert not other.alive("same") and other.sessions() == []
    other.start("same", SLEEP, cwd=run_dir)
    assert {s.pid for s in host.sessions()} != {s.pid for s in other.sessions()}
    other.stop("same")
    assert host.alive("same")


def test_long_run_dir_uses_short_socket(long_run_dir: Path) -> None:
    host = Host(long_run_dir)
    name = "t" * 100
    host.start(name, [PY, "-c", ECHO], cwd="/", terminal=True)
    assert stat.S_IMODE(long_run_dir.stat().st_mode) == 0o700
    sock = meta(long_run_dir, name)["socket"]
    assert sock.startswith("/tmp/burner-") and len(sock.encode()) <= 100
    assert stat.S_IMODE(os.stat(Path(sock).parent).st_mode) == 0o700
    wait_for(lambda: "ready" in host.screen(name))
    host.stop(name)
    assert not Path(sock).exists()
