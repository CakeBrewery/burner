from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest
from conftest import PY, wait_for

import burner
from burner import (
    AlreadyRunningError,
    ExitStatus,
    Host,
    HostError,
    NotRunningError,
    Session,
    UnsafeRunDirError,
    paths,
)

SLEEP = [PY, "-c", "import time; time.sleep(60)"]


def test_error_classes() -> None:
    assert issubclass(HostError, RuntimeError)
    for cls in (NotRunningError, AlreadyRunningError, UnsafeRunDirError):
        assert issubclass(cls, HostError)
    assert burner.client.HostError is HostError


def test_errors_name_the_session(host: Host, run_dir: Path) -> None:
    with pytest.raises(NotRunningError, match="^session web is not running$"):
        host.write("web", b"x")
    host.start("web", SLEEP, cwd=run_dir)
    with pytest.raises(AlreadyRunningError, match="^session web is already running$"):
        host.start("web", SLEEP, cwd=run_dir)


def test_exit_status(host: Host, run_dir: Path) -> None:
    assert host.exit_status("job") is None
    before = time.time()
    host.start("job", [PY, "-c", "raise SystemExit(3)"], cwd=run_dir)
    wait_for(lambda: host.gone("job"))
    status = host.exit_status("job")
    assert isinstance(status, ExitStatus)
    assert (status.code, status.signal) == (3, None)
    assert before <= status.ended <= time.time()
    host.start("job", SLEEP, cwd=run_dir)
    host.stop("job")
    assert host.exit_status("job").signal == signal.SIGTERM
    assert host.exit_status("job").code is None


def test_malformed_exit_record_reads_as_none(host: Host, run_dir: Path) -> None:
    (run_dir / "job.exit").write_text('{"code": "x", "time": 1}')
    assert host.exit_status("job") is None


def test_host_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "a" / "b"
    host = Host(missing)
    assert host.run_dir == missing
    assert not host.alive("web") and host.gone("web")
    assert host.sessions() == []
    assert host.exit_status("web") is None
    assert host.tail("web") == ""
    assert host.log_path("web") == missing / "web.log"
    host.stop("web")
    assert host.wait("web", timeout=1) is None
    assert not (tmp_path / "a").exists()


def test_default_run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BURNER_DIR", str(tmp_path / "env"))
    assert Host().run_dir == tmp_path / "env"
    monkeypatch.setenv("BURNER_DIR", "")
    assert Host().run_dir == Path.home() / ".burner"
    monkeypatch.delenv("BURNER_DIR")
    assert Host().run_dir == Path.home() / ".burner"
    assert not (tmp_path / "env").exists()


def test_start_returns_session_and_creates_run_dir(tmp_path: Path, run_dir: Path) -> None:
    fresh = run_dir / "x" / "y"
    host = Host(fresh, timeout=5)
    before = time.time()
    session = host.start("web", SLEEP, cwd=run_dir)
    try:
        assert isinstance(session, Session)
        assert session == host.sessions()[0]
        assert session.name == "web" and not session.terminal
        assert before <= session.started <= time.time()
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
    finally:
        host.stop("web")


def test_start_cwd_defaults_to_the_callers(
    host: Host, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(run_dir)
    host.start("pwd", [PY, "-c", "import os; print(os.getcwd())"])
    assert host.wait("pwd", timeout=10).code == 0
    assert Path(host.tail("pwd")).resolve() == run_dir.resolve()


NAME_HELP = "use 1 to 100 letters, digits, '.', '_', '-', starting with a letter or digit"


@pytest.mark.parametrize(
    ("args", "kwargs", "error", "match"),
    [
        ((5, SLEEP), {}, TypeError, "session name"),
        (("a b", SLEEP), {}, ValueError, f"^invalid session name 'a b': {NAME_HELP}$"),
        (("-x", SLEEP), {}, ValueError, "invalid session name"),
        (("x" * 101, SLEEP), {}, ValueError, "invalid session name"),
        (("web", "sleep 1"), {}, TypeError, "argv"),
        (("web", []), {}, ValueError, "argv"),
        (("web", [5]), {}, TypeError, "argv"),
        (("web", SLEEP), {"cwd": 5}, TypeError, "cwd"),
        (("web", SLEEP), {"env": "PATH=/bin"}, TypeError, "env"),
        (("web", SLEEP), {"env": {"A": 1}}, TypeError, "env"),
        (("web", SLEEP), {"cols": 0}, ValueError, "cols"),
        (("web", SLEEP), {"rows": 1001}, ValueError, "rows"),
        (("web", SLEEP), {"cols": 1000, "rows": 101}, ValueError, "100000 cells"),
        (("web", SLEEP), {"cols": "80"}, TypeError, "cols"),
        (("web", SLEEP), {"rows": True}, TypeError, "rows"),
        (("web", SLEEP), {"log": b"x.log"}, TypeError, "log"),
        (("web", SLEEP), {"terminal": True, "log": "x.log"}, ValueError, "log"),
        (("web", [PY, "a\0b"]), {}, ValueError, "argv"),
        (("web", SLEEP), {"cwd": "/tmp\0x"}, ValueError, "cwd"),
        (("web", SLEEP), {"log": "x\0.log"}, ValueError, "log"),
        (("web", SLEEP), {"env": {"A\0": "1"}}, ValueError, "env"),
        (("web", SLEEP), {"env": {"A": "1\0"}}, ValueError, "env"),
        (("web", SLEEP), {"env": {"": "1"}}, ValueError, "env"),
        (("web", SLEEP), {"env": {"A=B": "1"}}, ValueError, "env"),
        (("web", SLEEP), {"terminal": 1}, TypeError, "terminal"),
        (("web", SLEEP), {"terminal": "yes"}, TypeError, "terminal"),
    ],
)
def test_start_validation(tmp_path: Path, args, kwargs, error, match) -> None:
    run_dir = tmp_path / "run"
    with pytest.raises(error, match=match):
        Host(run_dir).start(*args, **kwargs)
    assert not run_dir.exists()


def test_largest_size_is_accepted(host: Host, run_dir: Path) -> None:
    host.start("big", SLEEP, cwd=run_dir, terminal=True, cols=1000, rows=100)
    host.stop("big")


@pytest.mark.parametrize(
    ("timeout", "error"), [(0, ValueError), (-1, ValueError), (float("nan"), ValueError),
                           ("1", TypeError), (None, TypeError)]
)
def test_host_timeout_validation(tmp_path: Path, timeout, error) -> None:
    with pytest.raises(error, match="timeout"):
        Host(tmp_path, timeout=timeout)


def test_start_reports_a_name_taken_during_start(
    host: Host, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir.joinpath("web.lock").touch()
    fd = os.open(run_dir / "web.lock", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        answers = iter([False])
        monkeypatch.setattr(host, "alive", lambda name: next(answers, True))
        with pytest.raises(AlreadyRunningError, match="^session web is already running$"):
            host.start("web", SLEEP, cwd=run_dir)
    finally:
        os.close(fd)
        (run_dir / "web.lock").unlink()


def test_os_error_does_not_escape_start(
    host: Host, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise OSError("no more processes")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(HostError, match="no more processes"):
        host.start("web", SLEEP, cwd=run_dir)


LOG_HELP = "^session web is a log session; read it with tail$"


def test_gone_session_rule(host: Host, run_dir: Path) -> None:
    calls = [
        lambda: host.screen("web"),
        lambda: host.press("web", "Enter"),
        lambda: host.send("web", "x"),
        lambda: host.write("web", b"x"),
        lambda: host.attach("web"),
    ]
    for call in calls:
        with pytest.raises(NotRunningError, match="^session web is not running$"):
            call()
    host.stop("web")
    host.start("web", SLEEP, cwd=run_dir)
    for call in calls:
        with pytest.raises(HostError, match=LOG_HELP):
            call()
    host.stop("web")
    for call in calls:
        with pytest.raises(NotRunningError):
            call()


def test_screen_after_the_end(host: Host, run_dir: Path) -> None:
    host.start("web", [PY, "-c", "print('bye')"], cwd=run_dir, terminal=True)
    wait_for(lambda: host.gone("web"))
    with pytest.raises(NotRunningError):
        host.screen("web")


@pytest.mark.parametrize(("lines", "error"), [(-1, ValueError), ("2", TypeError)])
def test_screen_lines_validation(host: Host, lines, error) -> None:
    with pytest.raises(error, match="lines"):
        host.screen("web", lines)


def test_unanswered_request(run_dir: Path) -> None:
    sock_path = run_dir / "web.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen()
    pid = os.getpid()
    meta = {"pid": pid, "child": pid, "pgid": pid, "terminal": True, "started": time.time(),
            "socket": str(sock_path)}
    (run_dir / "web.json").write_text(json.dumps(meta))
    fd = os.open(run_dir / "web.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(HostError, match="^session web did not answer within 0.2s$"):
            Host(run_dir, timeout=0.2).screen("web")
    finally:
        os.close(fd)
        listener.close()


def test_send_and_write_validation(host: Host, run_dir: Path) -> None:
    host.start("web", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir, terminal=True)
    for text in ("a\nb", "a\rb"):
        with pytest.raises(ValueError, match="newline"):
            host.send("web", text)
    with pytest.raises(TypeError, match="text"):
        host.send("web", b"x")
    with pytest.raises(TypeError, match="data"):
        host.write("web", "x")
    host.write("web", bytearray(b"x"))
    assert not hasattr(host, "send_line") and not hasattr(host, "log_tail")


def test_tail(host: Host, run_dir: Path) -> None:
    (run_dir / "web.log").write_text("a\nb\nc\n")
    assert host.tail("web") == "a\nb\nc"
    assert host.tail("web", 2) == "b\nc"
    assert host.tail("web", 0) == ""
    with pytest.raises(ValueError, match="lines"):
        host.tail("web", -1)
    with pytest.raises(TypeError, match="lines"):
        host.tail("web", "2")
    (run_dir / "dir.log").mkdir()
    with pytest.raises(HostError):
        host.tail("dir")


def test_wait(host: Host, run_dir: Path) -> None:
    assert host.wait("web") is None
    host.start("web", [PY, "-c", "import time; time.sleep(0.3); raise SystemExit(4)"],
               cwd=run_dir)
    status = host.wait("web")
    assert status == host.exit_status("web") and status.code == 4
    host.start("web", SLEEP, cwd=run_dir)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="^session web is still running after 0.2s$"):
        host.wait("web", timeout=0.2)
    assert time.monotonic() - started < 2
    host.stop("web")
    assert host.wait("web", timeout=0).signal == signal.SIGTERM


@pytest.mark.parametrize(
    ("value", "error"), [(-1, ValueError), (float("inf"), ValueError), ("1", TypeError)]
)
def test_wait_and_stop_validation(host: Host, value, error) -> None:
    with pytest.raises(error, match="timeout"):
        host.wait("web", timeout=value)
    with pytest.raises(error, match="grace"):
        host.stop("web", grace=value)


def test_press_checks_keys_first(host: Host) -> None:
    with pytest.raises(ValueError, match="unknown key 'Hyper'"):
        host.press("web", "Enter", "Hyper")
    with pytest.raises(TypeError, match="key"):
        host.press("web", 5)


def test_wait_stops_when_it_cannot_tell(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "lock_probe", lambda path: None)
    started = time.monotonic()
    with pytest.raises(HostError, match="^cannot tell whether session web is running$"):
        host.wait("web")
    assert time.monotonic() - started < 1


def test_wait_with_a_zero_timeout_does_not_call_an_unknown_state_running(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "lock_probe", lambda path: None)
    with pytest.raises(HostError, match="cannot tell"):
        host.wait("web", timeout=0)
