from __future__ import annotations

import json
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from conftest import ECHO, PY, wait_for

import burner
from burner import Host

SLEEP = [PY, "-c", "import time; time.sleep(60)"]
COMMANDS = ("start", "ls", "alive", "wait", "stop", "screen", "tail", "press", "send", "attach")


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "burner", *args], capture_output=True, text=True, timeout=20, env=env
    )


def cli(run_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return run("--run-dir", str(run_dir), *args)


def one_error_line(result: subprocess.CompletedProcess) -> str:
    assert result.stderr.startswith("burner: ") and result.stderr.count("\n") == 1, result.stderr
    return result.stderr


def test_terminal_commands(run_dir: Path) -> None:
    assert cli(run_dir, "start", "--terminal", "--cwd", str(run_dir), "tui", "--",
               PY, "-c", ECHO).returncode == 0
    assert cli(run_dir, "alive", "tui").returncode == 0
    wait_for(lambda: "ready" in cli(run_dir, "screen", "tui").stdout)
    assert cli(run_dir, "send", "tui", "ab").returncode == 0
    assert cli(run_dir, "send", "tui", "--", "-n").returncode == 0
    assert cli(run_dir, "press", "tui", "Up", "C-c").returncode == 0
    wait_for(lambda: "got:1b5b4103" in cli(run_dir, "screen", "tui").stdout)
    assert "got:2d6e\ngot:0d" in cli(run_dir, "screen", "tui").stdout
    assert cli(run_dir, "screen", "tui", "-n", "1").stdout == "got:1b5b4103\n"
    assert cli(run_dir, "screen", "tui", "--lines", "1").stdout == "got:1b5b4103\n"
    assert cli(run_dir, "stop", "tui", "--grace", "1").returncode == 0
    assert cli(run_dir, "alive", "tui").returncode == 1
    assert cli(run_dir, "ls").stdout == ""


def test_log_commands_and_env_run_dir(run_dir: Path) -> None:
    env = {**os.environ, "BURNER_DIR": str(run_dir)}
    script = "import time\nfor i in range(5): print(i, flush=True)\ntime.sleep(0.5)"
    result = run("start", "svc", "--", PY, "-c", script, env=env)
    assert result.returncode == 0, result.stderr
    follow = run("tail", "-f", "svc", env=env)
    assert follow.stdout.split() == ["0", "1", "2", "3", "4"]
    assert cli(run_dir, "tail", "svc", "-n", "2").stdout == "3\n4\n"
    assert cli(run_dir, "tail", "svc", "--lines", "1").stdout == "4\n"


def test_options_before_the_separator_belong_to_start(run_dir: Path) -> None:
    script = "import sys, time; print(sys.argv[1:], flush=True); time.sleep(60)"
    assert cli(run_dir, "start", "x", "--terminal", "--",
               PY, "-c", script, "--terminal", "--").returncode == 0
    host = Host(run_dir)
    [session] = host.sessions()
    assert session.terminal
    wait_for(lambda: "['--terminal', '--']" in host.screen("x"))
    host.stop("x")


def test_size_sets_the_terminal(run_dir: Path) -> None:
    script = "import os, time; print(os.get_terminal_size()); time.sleep(60)"
    assert cli(run_dir, "start", "--terminal", "--size", "100x30", "s", "--",
               PY, "-c", script).returncode == 0
    wait_for(lambda: "columns=100, lines=30" in cli(run_dir, "screen", "s").stdout)


def test_run_dir_after_the_command(run_dir: Path) -> None:
    assert run("start", "--run-dir", str(run_dir), "svc", "--", *SLEEP).returncode == 0
    assert run("alive", "svc", "--run-dir", str(run_dir)).returncode == 0
    assert run("ls", "--run-dir", str(run_dir)).stdout.startswith("svc\t")
    other = run_dir / "other"
    assert run("--run-dir", str(other), "alive", "svc", "--run-dir", str(run_dir)).returncode == 0
    assert run("--run-dir", str(run_dir), "alive", "svc", "--run-dir", str(other)).returncode == 1
    assert not other.exists()


def test_ls_formats(run_dir: Path) -> None:
    host = Host(run_dir)
    host.start("b", SLEEP, cwd=run_dir, terminal=True)
    host.start("a", SLEEP, cwd=run_dir)
    sessions = host.sessions()
    rows = [line.split("\t") for line in cli(run_dir, "ls").stdout.splitlines()]
    assert [row[:3] for row in rows] == [
        [s.name, str(s.pid), "terminal" if s.terminal else "log"] for s in sessions
    ]
    assert [r[0] for r in rows] == ["a", "b"]
    for row, s in zip(rows, sessions, strict=True):
        started = datetime.fromisoformat(row[3])
        assert started.tzinfo is not None
        assert abs(started.timestamp() - s.started) < 1
    data = json.loads(cli(run_dir, "ls", "--json").stdout)
    assert data == [
        {"name": r[0], "pid": int(r[1]), "kind": r[2], "started": r[3]} for r in rows
    ]
    for name in ("a", "b"):
        host.stop(name)
    assert json.loads(cli(run_dir, "ls", "--json").stdout) == []


def test_alive_exit_codes(run_dir: Path) -> None:
    assert cli(run_dir, "alive", "x").returncode == 1
    for args in (("alive", "bad name"), ("alive", "x" * 101)):
        result = cli(run_dir, *args)
        assert result.returncode == 2
        one_error_line(result)


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a lock file of mode 000")
def test_alive_and_wait_cannot_tell(run_dir: Path) -> None:
    (run_dir / "x.lock").touch(mode=0o000)
    result = cli(run_dir, "alive", "x")
    assert result.returncode == 2
    assert "cannot tell" in one_error_line(result)
    result = cli(run_dir, "wait", "x")
    assert result.returncode == 125
    assert "cannot tell" in one_error_line(result)


def test_wait_exit_codes(run_dir: Path) -> None:
    host = Host(run_dir)
    host.start("code", [PY, "-c", "raise SystemExit(3)"], cwd=run_dir)
    assert cli(run_dir, "wait", "code").returncode == 3
    host.start("sig", SLEEP, cwd=run_dir)
    result = cli(run_dir, "wait", "sig", "--timeout", "0.2")
    assert result.returncode == 124
    assert one_error_line(result) == "burner: session sig is still running after 0.2s\n"
    os.killpg(host.sessions()[0].pid, signal.SIGTERM)
    assert cli(run_dir, "wait", "sig").returncode == 128 + signal.SIGTERM
    result = cli(run_dir, "wait", "never")
    assert result.returncode == 125
    assert "no exit record" in one_error_line(result)
    assert cli(run_dir, "wait", "bad name").returncode == 125


def test_ctrl_c_exits_130(run_dir: Path) -> None:
    script = "import time\nprint('ready', flush=True)\ntime.sleep(60)"
    Host(run_dir).start("svc", [PY, "-c", script], cwd=run_dir)
    proc = subprocess.Popen([PY, "-m", "burner", "--run-dir", str(run_dir), "tail", "-f", "svc"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # The first line shows that the command is past startup and inside its loop.
    assert proc.stdout.readline() == b"ready\n"
    proc.send_signal(signal.SIGINT)
    assert proc.wait(10) == 130
    assert proc.stderr.read() == b""


def test_errors_are_one_line(run_dir: Path) -> None:
    for args in (("press", "nope", "Enter"), ("start", "x", "--", "no-such-program-xyz"),
                 ("screen", "nope")):
        result = cli(run_dir, *args)
        assert result.returncode == 1, args
        one_error_line(result)
    assert cli(run_dir, "stop", "nope").returncode == 0


def test_bad_values_are_usage_errors(run_dir: Path) -> None:
    for args in (("start", "bad name", "--", "true"), ("press", "x", "Hyper"),
                 ("start", "--size", "0x5", "x", "--", "true"), ("screen", "bad name"),
                 ("tail", "bad name"), ("stop", "bad name")):
        result = cli(run_dir, *args)
        assert result.returncode == 2, args
        one_error_line(result)
    assert not list(run_dir.iterdir())


def test_start_without_separator(run_dir: Path) -> None:
    for args in (("start", "x", "sleep", "1"), ("start", "x", "--bogus", "sleep")):
        result = cli(run_dir, *args)
        assert result.returncode == 2
        assert one_error_line(result) == "burner: start needs -- before the program\n"


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("nope",),
        ("start", "x"),
        ("start", "x", "true"),
        ("start", "x", "--"),
        ("start", "--size", "big", "x", "--", "true"),
        ("send", "x"),
        ("send", "x", "a", "--", "b"),
        ("send", "x", "--", "a", "b"),
        ("press", "x"),
        ("alive",),
        ("stop", "x", "--", "y"),
        ("ls", "--grace", "1"),
        ("start", "--term", "x", "--", "sleep", "1"),
        ("--run", "/tmp", "ls"),
        ("ls", "--js"),
        ("screen", "x", "-n", "-1"),
        ("screen", "x", "-n", "two"),
        ("tail", "x", "-n", "-1"),
        ("stop", "x", "--grace", "-1"),
        ("stop", "x", "--grace", "nan"),
        ("stop", "x", "--grace", "inf"),
        ("wait", "x", "--timeout", "-1"),
        ("wait", "x", "--timeout", "nan"),
    ],
)
def test_usage_errors(run_dir: Path, args: tuple[str, ...]) -> None:
    result = cli(run_dir, *args)
    assert result.returncode == 2, result
    one_error_line(result)
    assert result.stdout == ""


@pytest.mark.parametrize("command", COMMANDS)
def test_help_has_an_example(command: str) -> None:
    result = run(command, "--help")
    assert result.returncode == 0
    assert "example:" in result.stdout.lower()
    assert f"burner {command} " in result.stdout


def test_top_help_and_version() -> None:
    result = run("--help")
    assert result.returncode == 0
    for command in COMMANDS:
        assert command in result.stdout
    version = run("--version")
    assert version.returncode == 0 and version.stdout == f"burner {burner.__version__}\n"


def test_press_help_lists_keys() -> None:
    text = run("press", "--help").stdout
    for key in ("BackTab", "Insert", "F12", "C-a"):
        assert key in text


def test_cli_module_runs() -> None:
    result = subprocess.run([PY, "-m", "burner.cli", "--version"], capture_output=True,
                            text=True, timeout=20)
    assert result.stdout == f"burner {burner.__version__}\n"


def test_closed_pipe_ends_quietly(run_dir: Path) -> None:
    script = "import time\nwhile True:\n    print('line', flush=True)\n    time.sleep(0.05)"
    Host(run_dir).start("web", [PY, "-c", script], cwd=run_dir)
    proc = subprocess.Popen([PY, "-m", "burner", "--run-dir", str(run_dir), "tail", "-f", "web"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout.readline()
    proc.stdout.close()
    assert proc.wait(10) == 141
    assert proc.stderr.read() == b""


def test_empty_screen_and_missing_log_print_nothing(run_dir: Path) -> None:
    Host(run_dir).start("blank", SLEEP, cwd=run_dir, terminal=True)
    result = cli(run_dir, "screen", "blank")
    assert (result.returncode, result.stdout) == (0, "")
    result = cli(run_dir, "tail", "nolog")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("unbuffered", ["", "1"])
@pytest.mark.parametrize("command", [["ls"], ["ls", "--json"], ["--version"], ["--help"]])
def test_closed_pipe_ends_quietly_for_every_output(
    run_dir: Path, command: list[str], unbuffered: str
) -> None:
    Host(run_dir).start("web", [PY, "-c", "import time; time.sleep(60)"], cwd=run_dir)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = subprocess.run([PY, "-m", "burner", "--run-dir", str(run_dir), *command],
                                stdout=write_end, stderr=subprocess.PIPE, timeout=20,
                                env={**os.environ, "PYTHONUNBUFFERED": unbuffered})
    finally:
        os.close(write_end)
    assert result.returncode == 141
    assert result.stderr == b""


def test_error_stays_on_one_line_for_an_argument_with_a_line_break(run_dir: Path) -> None:
    result = cli(run_dir, "ls", "unexpected\nargument")
    assert result.returncode == 2
    assert one_error_line(result) == "burner: unrecognized arguments: unexpected argument\n"
