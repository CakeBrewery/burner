"""Command line interface: ``burner [--run-dir DIR] COMMAND``."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

from . import __version__
from .client import Host, Session
from .errors import HostError

FOLLOW_INTERVAL = 0.2
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_BURNER = 125
EXIT_INTERRUPT = 130
EXIT_PIPE = 141
# Commands whose exit status is the child's, so their own errors use EXIT_BURNER.
CHILD_STATUS = ("wait", "attach")

KEYS_HELP = """\
keys: Up Down Left Right Home End PageUp PageDown Insert Delete
      Enter Escape Tab BackTab Backspace Space F1..F12 C-a..C-z
      Names match without regard to case."""

TOP_HELP = """\
The run dir is --run-dir, else $BURNER_DIR, else ~/.burner.

exit status: 0 success, 1 error, 2 usage error or bad value, 130 interrupted,
141 when the reader of the output went away.
wait and attach exit with the child's status: its code, or 128+N for
signal N; 124 when wait times out, 125 on a burner error.

example:
  burner start web -- python3 -m http.server 8000
  burner tail -f web"""


class UsageError(Exception):
    """The command line is wrong."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)

    def _print_message(self, message: str, file: Any = None) -> None:
        # argparse drops write errors here. Let them through, so help text
        # sent to a closed reader ends like any other output.
        if message:
            (file or sys.stderr).write(message)


def _parser() -> _Parser:
    p = _Parser(
        prog="burner",
        description="Run named processes that keep running after the starter exits.",
        epilog=TOP_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    p.add_argument("--run-dir", metavar="DIR", help="run dir (default: $BURNER_DIR or ~/.burner)")
    p.add_argument("--version", action="version", version=f"burner {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def command(name: str, text: str, example: str, epilog: str = "") -> _Parser:
        parser = sub.add_parser(
            name,
            help=text,
            description=text[0].upper() + text[1:] + ".",
            epilog=f"{epilog}\n\nexample:\n  {example}".strip(),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
        )
        parser.add_argument("--run-dir", metavar="DIR", default=argparse.SUPPRESS,
                            help="run dir (default: $BURNER_DIR or ~/.burner)")
        return parser

    s = command("start", "start a session",
                "burner start --terminal repl -- python3 -q",
                "Everything after -- is the program and its arguments.")
    s.usage = "burner start [options] NAME -- PROGRAM [ARG...]"
    s.add_argument("--terminal", action="store_true", help="run the program on a terminal")
    s.add_argument("--cwd", metavar="DIR", help="working directory (default: the current one)")
    s.add_argument("--log", metavar="FILE", help="log file of a log session "
                   "(default: RUN_DIR/NAME.log)")
    s.add_argument("--size", metavar="COLSxROWS", type=_size, default=(80, 24),
                   help="terminal size (default: 80x24)")
    s.add_argument("name", metavar="NAME")

    s = command("ls", "list running sessions", "burner ls --json",
                "Prints NAME, PID, KIND, and STARTED separated by tabs, sorted by name.")
    s.add_argument("--json", action="store_true", help="print a JSON list")

    s = command("alive", "tell whether a session is running", "burner alive web",
                "Exits 0 when it runs, 1 when it does not, and 2 when that cannot be told.")
    s.add_argument("name", metavar="NAME")

    s = command("wait", "wait for a session to end", "burner wait job --timeout 60",
                "Exits with the child's status: its code, or 128+N for signal N.\n"
                "Exits 124 on timeout and 125 on a burner error.")
    s.add_argument("name", metavar="NAME")
    s.add_argument("--timeout", metavar="S", type=_seconds, help="give up after S seconds")

    s = command("stop", "stop a session", "burner stop web --grace 10",
                "Does nothing when the session is not running.")
    s.add_argument("name", metavar="NAME")
    s.add_argument("--grace", metavar="S", type=_seconds, default=5.0,
                   help="seconds before SIGKILL (default: 5)")

    s = command("screen", "print the screen of a terminal session", "burner screen repl -n 5")
    s.add_argument("name", metavar="NAME")
    s.add_argument("-n", "--lines", metavar="N", type=_count, help="print only the last N rows")

    s = command("tail", "print the end of a session's log", "burner tail -f web")
    s.add_argument("name", metavar="NAME")
    s.add_argument("-n", "--lines", metavar="N", type=_count, default=30,
                   help="print the last N lines (default: 30)")
    s.add_argument("-f", "--follow", action="store_true",
                   help="keep printing new output until the session ends")

    s = command("press", "press keys in a terminal session", "burner press repl Up Enter",
                KEYS_HELP)
    s.add_argument("name", metavar="NAME")
    s.add_argument("keys", metavar="KEY", nargs="+")

    s = command("send", "type a line of text into a terminal session, then press Enter",
                "burner send repl -- 'print(6 * 7)'",
                "Put -- before TEXT when it starts with -.")
    s.usage = "burner send NAME [--] TEXT"
    s.add_argument("name", metavar="NAME")
    s.add_argument("text", metavar="TEXT", nargs="?")

    s = command("attach", "connect this terminal to a terminal session", "burner attach repl",
                "Ctrl-\\ detaches. Exits with the child's status when the session ends\n"
                "and 0 after a detach.")
    s.add_argument("name", metavar="NAME")

    s = command("clean", "remove the files of ended sessions", "burner clean web",
                "Prints the name of each session it cleaned, one per line. A running\n"
                "session keeps its files; naming one is an error.")
    s.add_argument("name", metavar="NAME", nargs="?")
    return p


def _size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text)
    if not match:
        raise argparse.ArgumentTypeError(f"size must look like 80x24, not {text!r}")
    return int(match[1]), int(match[2])


def _count(text: str) -> int:
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError(f"expected a whole number >= 0, not {text!r}")
    return int(text)


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"expected seconds >= 0, not {text!r}")
    return value


def parse(argv: list[str]) -> argparse.Namespace:
    """Parse a command line. Raises UsageError."""
    # Split at the first -- before argparse runs, so start's options never
    # reach the program and the program's arguments never reach argparse.
    rest: list[str] | None = None
    if "--" in argv:
        cut = argv.index("--")
        argv, rest = argv[:cut], argv[cut + 1 :]
    args, extra = _parser().parse_known_args(argv)
    if args.command == "start" and rest is None:
        raise UsageError("start needs -- before the program")
    if extra:
        raise UsageError(f"unrecognized arguments: {' '.join(extra)}")
    if args.command == "start":
        if not rest:
            raise UsageError("give the program after --")
        args.program = rest
    elif args.command == "send":
        if rest is not None:
            if args.text is not None or len(rest) != 1:
                raise UsageError("send takes one TEXT")
            args.text = rest[0]
        elif args.text is None:
            raise UsageError("the following arguments are required: TEXT")
    elif rest is not None:
        raise UsageError(f"{args.command} takes no arguments after --")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit status."""
    try:
        args = parse(sys.argv[1:] if argv is None else list(argv))
    except BrokenPipeError:  # Help or version text written to a closed reader.
        return _reader_gone()
    except UsageError as exc:
        _error(exc)
        return EXIT_USAGE
    except SystemExit as exc:  # --help and --version
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            return _reader_gone()
        return exc.code if isinstance(exc.code, int) else 0
    child_status = args.command in CHILD_STATUS
    failed = EXIT_BURNER if child_status else EXIT_ERROR
    # A bad value is a usage error. alive exits 1 only when the session is
    # known not to run, so its every error exits 2.
    bad_value = EXIT_BURNER if child_status else EXIT_USAGE
    if args.command == "alive":
        failed = EXIT_USAGE
    try:
        status = _run(Host(args.run_dir), args)
        sys.stdout.flush()  # A reader that went away shows up here, not at exit.
        return status
    except BrokenPipeError:
        return _reader_gone()
    except TimeoutError as exc:
        _error(exc)
        return EXIT_TIMEOUT if args.command == "wait" else failed
    except (ValueError, TypeError) as exc:
        _error(exc)
        return bad_value
    except (HostError, OSError) as exc:
        _error(exc)
        return failed
    except KeyboardInterrupt:
        return EXIT_INTERRUPT


def _error(message: object) -> None:
    """Print one error line, whatever line breaks the message holds."""
    print("burner:", " ".join(str(message).splitlines()), file=sys.stderr)


def _reader_gone() -> int:
    """Handle a reader that went away, as with ``burner tail -f web | head``.

    Send what is left in the stdout buffer to /dev/null so the exit stays quiet.
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)
    return EXIT_PIPE


def _run(host: Host, args: argparse.Namespace) -> int:
    if args.command == "start":
        cols, rows = args.size
        host.start(args.name, args.program, cwd=args.cwd, terminal=args.terminal,
                   cols=cols, rows=rows, log=args.log)
    elif args.command == "ls":
        _ls(host.sessions(), args.json)
    elif args.command == "alive":
        if host.alive(args.name):
            return 0
        if host.gone(args.name):
            return 1
        print(f"burner: cannot tell whether session {args.name} is running", file=sys.stderr)
        return 2
    elif args.command == "wait":
        status = host.wait(args.name, args.timeout)
        if status is None:
            raise HostError(f"session {args.name} has no exit record")
        return status.code if status.code is not None else 128 + (status.signal or 0)
    elif args.command == "stop":
        host.stop(args.name, grace=args.grace)
    elif args.command == "screen":
        text = host.screen(args.name, args.lines)
        if text:
            print(text)
    elif args.command == "tail":
        return _tail(host, args.name, args.lines, args.follow)
    elif args.command == "press":
        host.press(args.name, *args.keys)
    elif args.command == "send":
        host.send(args.name, args.text)
    elif args.command == "attach":
        return host.attach(args.name)
    elif args.command == "clean":
        for name in host.clean(args.name):
            print(name)
    return 0


def _ls(sessions: list[Session], as_json: bool) -> None:
    rows = [
        {
            "name": s.name,
            "pid": s.pid,
            "kind": "terminal" if s.terminal else "log",
            "started": datetime.fromtimestamp(s.started).astimezone().isoformat("T", "seconds"),
        }
        for s in sessions
    ]
    if as_json:
        print(json.dumps(rows))
    else:
        for row in rows:
            print("\t".join(str(value) for value in row.values()))


def _tail(host: Host, name: str, lines: int, follow: bool) -> int:
    path = host.log_path(name)
    text = host.tail(name, lines)
    if text:
        print(text, flush=True)
    if not follow:
        return 0
    try:
        pos = path.stat().st_size
    except FileNotFoundError:
        pos = 0
    while True:
        ended = not host.alive(name)
        try:
            with open(path, "rb") as f:
                if f.seek(0, os.SEEK_END) < pos:
                    pos = 0
                f.seek(pos)
                data = f.read()
        except FileNotFoundError:
            data = b""
        pos += len(data)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
        if ended:
            return 0
        time.sleep(FOLLOW_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
