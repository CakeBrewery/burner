"""Record a program's raw output and tmux's rendering of it, then compare them with Screen.

Usage:
    python tools/record.py [--drop-env PREFIX]... OUTDIR NAME 'COMMAND' STEP...

Each STEP is one of:
    capture:LABEL   wait for the output to go quiet, then save LABEL.bin and LABEL.txt
    keys:K1,K2      send keys with tmux send-keys (key names such as Down or Enter)
    sleep:SECONDS   wait

LABEL.bin holds the raw bytes the program wrote up to the capture. LABEL.txt holds
`tmux capture-pane -p` at the same moment. The script prints whether Screen agrees.

The recorded program gets the caller's environment without the variables of the
caller's terminal. Each --drop-env PREFIX also removes the variables whose names
start with PREFIX.

The script runs its own tmux server on a socket in a fresh temporary folder, so it
never touches another tmux server. It needs tmux in PATH or at TMUX_BIN.
"""

from __future__ import annotations

import argparse
import difflib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from burner.screen import Screen  # noqa: E402

COLS, ROWS = 80, 24
QUIET = 1.5
TIMEOUT = 30.0
# Variables of the caller's terminal, which the recorded program must not see.
TERMINAL_ENV = ("TMUX", "TERM_PROGRAM", "TERMINFO", "COLORTERM")


def scrub_env(env: dict[str, str], prefixes: list[str]) -> dict[str, str]:
    """Drop the caller's terminal variables and those that start with a prefix."""
    drop = TERMINAL_ENV + tuple(prefixes)
    return {k: v for k, v in env.items() if not k.startswith(drop)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="record.py")
    parser.add_argument("--drop-env", metavar="PREFIX", action="append", default=[],
                        help="also remove variables whose names start with PREFIX")
    parser.add_argument("outdir")
    parser.add_argument("name")
    parser.add_argument("command")
    parser.add_argument("steps", nargs="+", metavar="STEP")
    args = parser.parse_args(argv)
    name, command, steps = args.name, args.command, args.steps
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    tmux_bin = os.environ.get("TMUX_BIN") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    env = scrub_env(dict(os.environ), args.drop_env)
    folder = tempfile.mkdtemp(prefix="sh.", dir="/tmp")
    sock = os.path.join(folder, "s")
    raw = os.path.join(folder, "raw")
    base = [tmux_bin, "-S", sock, "-f", "/dev/null"]

    def tmux(*args: str) -> str:
        return subprocess.run(
            base + list(args), env=env, check=True, capture_output=True, text=True
        ).stdout

    # tmux sets these in every pane; a burner child does not get them.
    unset = "unset TMUX TMUX_PANE TERM_PROGRAM TERM_PROGRAM_VERSION"
    inner = f"{unset}; sleep 1; {command}; sleep 3600"
    tmux(
        "start-server", ";",
        "set", "-g", "default-terminal", "xterm-256color", ";",
        "set", "-g", "status", "off", ";",
        "new-session", "-d", "-s", "rec", "-x", str(COLS), "-y", str(ROWS),
        "sh -c " + shlex.quote(inner),
    )  # fmt: skip
    ok = True
    try:
        tmux("pipe-pane", "-t", "rec", "-o", "cat > " + shlex.quote(raw))
        for step in steps:
            kind, _, value = step.partition(":")
            if kind == "sleep":
                time.sleep(float(value))
            elif kind == "keys":
                tmux("send-keys", "-t", "rec", *value.split(","))
            elif kind == "capture":
                data = _wait_quiet(raw)
                shown = tmux("capture-pane", "-p", "-t", "rec")
                ok &= _save_and_compare(out, f"{name}.{value}", data, shown)
    finally:
        subprocess.run(base + ["kill-server"], env=env, capture_output=True)
        shutil.rmtree(folder, ignore_errors=True)
    return 0 if ok else 1


def _wait_quiet(path: str) -> bytes:
    deadline = time.monotonic() + TIMEOUT
    size, since = -1, time.monotonic()
    while time.monotonic() < deadline:
        now = os.path.getsize(path) if os.path.exists(path) else 0
        if now != size:
            size, since = now, time.monotonic()
        elif size > 0 and time.monotonic() - since >= QUIET:
            break
        time.sleep(0.1)
    return Path(path).read_bytes()


def _trim(text: str) -> list[str]:
    rows = [line.rstrip(" ") for line in text.split("\n")]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _save_and_compare(out: Path, stem: str, data: bytes, shown: str) -> bool:
    (out / f"{stem}.bin").write_bytes(data)
    (out / f"{stem}.txt").write_text("\n".join(_trim(shown)) + "\n")
    screen = Screen(COLS, ROWS)
    screen.feed(data)
    want, got = _trim(shown), screen.text().split("\n") if screen.text() else []
    if want == got:
        print(f"{stem}: match ({len(data)} bytes, {len(want)} rows)")
        return True
    print(f"{stem}: MISMATCH ({len(data)} bytes)")
    sys.stdout.writelines(
        line + "\n" for line in difflib.unified_diff(want, got, "tmux", "screen", lineterm="")
    )
    return False


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
