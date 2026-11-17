<p align="center">
  <img src="docs/burner.svg" width="128" alt="burner icon: a red circle with a black spiral">
</p>

# burner

burner keeps a named process running after the program that started it
exits. Any process of the same user can then use it by name: check that it
runs, read its screen, type into it, read its log, wait for its end, stop
it, or attach a terminal to it.

burner is a Python library and a command line tool. It runs on macOS and
Linux and needs only the Python 3.11+ standard library.

## Why the name

A burner is the ring on a stove that you cook on. This one is for AI agents
to cook on. An agent starts a dev server, a test run, or a full-screen
program on a burner, leaves it running, and comes back to it by name: it
reads the screen as plain text, presses keys, reads the log, and stops the
session when the work is done. Nothing here is specific to AI. A script or
a person uses the same commands.

## How it differs

- **nohup** and `&` keep a process running, but give it no name, no screen,
  and no input. You track pids and log files yourself.
- **tmux** and **screen** keep programs on terminals inside one server that
  holds every session. burner has no server. Each session has its own small
  host process, so one failure ends one session, and no global server exists
  to kill by mistake.
- **dtach** and **abduco** also use one process per session, but they only
  attach. burner also renders the screen as text, presses keys, keeps logs,
  records how the child ended, and gives programs a typed API.

## Install

```
pip install "burner @ git+https://github.com/CakeBrewery/burner.git@v0.2.0"
```

Warning: `pip install burner` installs an unrelated project from PyPI.

## Quick start

On the command line:

```
burner start web -- python3 -m http.server 8000
burner ls
burner tail -f web                  # Ctrl-C stops following; web keeps running
burner stop web

burner start --terminal repl -- python3 -q
burner send repl 'print(6 * 7)'
burner screen repl
burner attach repl                  # Ctrl-\ detaches
```

In Python:

```python
import time

from burner import Host, Key

host = Host()  # the run dir is $BURNER_DIR, else ~/.burner

# A terminal session: read its screen and type into it.
host.start("repl", ["python3", "-q"], terminal=True)
host.send("repl", "print(6 * 7)")
while "42" not in host.screen("repl"):
    time.sleep(0.1)
host.press("repl", Key.ENTER, "C-d")
print(host.wait("repl", timeout=10))  # ExitStatus(code=0, signal=None, ended=...)

# A log session: its output goes to a file that outlives it.
host.start("job", ["python3", "-c", "print('hello from a log session')"])
host.wait("job", timeout=10)
print(host.tail("job"))

# Stop a session that runs until told otherwise.
session = host.start("sleeper", ["sleep", "600"])
print([s.name for s in host.sessions()])  # ['sleeper']
host.stop("sleeper")
print(host.exit_status("sleeper").signal)  # the signal that ended it
```

## Two kinds of session

- **terminal** (`terminal=True`): the child runs on a PTY, 80x24 by default.
  The host keeps a screen model of the PTY output, so clients can read the
  rendered text, send input, and attach. Use it for full-screen programs and
  prompts.
- **log** (`terminal=False`, the default): the child's stdin is `/dev/null`
  and its stdout and stderr go to a log file. There is no PTY. Use it for
  services and dev servers. The log outlives the session, so a crash leaves
  its output behind.

`env` is the child's whole environment. Without it the child gets the
caller's environment. Set `TERM` yourself for terminal sessions.

## Python API

`Host(run_dir=None, *, timeout=15.0)` works on the sessions in one run dir.
The run dir is `run_dir`, else `$BURNER_DIR`, else `~/.burner`. Making a
`Host` creates nothing on disk; the first `start` creates the run dir with
mode 0700. `timeout` bounds each request to a session's host, in seconds.

| Method | Returns | Raises | After the session ends |
| --- | --- | --- | --- |
| `start(name, argv, *, cwd=None, env=None, terminal=False, cols=80, rows=24, log=None)` | `Session` | `AlreadyRunningError`; `HostError` when the program cannot start | |
| `alive(name)` | `True` when known running | | `False` |
| `gone(name)` | `True` when known not running | | `True` |
| `sessions()` | `list[Session]` of running sessions, sorted by name | | not listed |
| `stop(name, *, grace=5.0)` | `None` once the session is gone | `HostError` when it still runs | does nothing |
| `wait(name, timeout=None)` | `ExitStatus`, or `None` without an exit record | `TimeoutError` when it still runs after `timeout`; `HostError` when the check keeps failing | returns at once |
| `exit_status(name)` | `ExitStatus` of the last run, or `None` | | works |
| `screen(name, lines=None)` | the screen text, or its last `lines` rows | | `NotRunningError` |
| `press(name, *keys)` | `None` | `ValueError` for an unknown key | `NotRunningError` |
| `send(name, text)` | `None`; types `text`, then presses Enter | `ValueError` when `text` holds a newline | `NotRunningError` |
| `write(name, data)` | `None`; writes raw bytes | | `NotRunningError` |
| `attach(name)` | 0 after a detach, else the child's status | `HostError` without a terminal on stdin and stdout | `NotRunningError` |
| `log_path(name)` | `Path` of the log file | | works |
| `tail(name, lines=30)` | the last `lines` lines of the log | | works |
| `clean(name=None)` | `list[str]` of the names it cleaned | `HostError` when `name` is running | removes the files the session left |

Rules that hold for every method:

- `HostError` is the base of `NotRunningError`, `AlreadyRunningError`, and
  `UnsafeRunDirError`. `screen`, `press`, `send`, `write`, and `attach` on a
  log session raise `HostError`.
- A bad value raises `ValueError` and a wrong type raises `TypeError`, before
  anything changes. A session name is 1 to 100 letters, digits, `.`, `_`, or
  `-`, and starts with a letter or digit. A terminal is 1 to 1000 cells on
  each side and at most 100,000 cells in all. `log` is for log sessions only.
- `OSError` never escapes, except the builtin `TimeoutError` from `wait`.
- The run dir must be private to you. Every method raises
  `UnsafeRunDirError` for a run dir that other users can change; see
  [Files in the run dir](#files-in-the-run-dir).
- `alive` and `gone` are both `False` when the check itself fails, so
  neither is a guess. Start a replacement only when `gone` is `True`.

`Session` is a frozen dataclass with `name`, `pid` (the child, which also
names its process group), `terminal`, and `started` (seconds since the
epoch). `ExitStatus` is a frozen dataclass with `code` (the exit code, or
`None` when a signal ended the child), `signal` (that signal, or `None`), and
`ended` (seconds since the epoch).

Key names for `press`, also as `Key` members: `Up` `Down` `Left` `Right`
`Home` `End` `PageUp` `PageDown` `Insert` `Delete` `Enter` `Escape` `Tab`
`BackTab` `Backspace` `Space` `F1` to `F12`. Control combinations `C-a` to
`C-z` are strings only, as in `host.press("repl", "C-a")`. Names match
without regard to case. Arrow keys follow the program's cursor key mode.

`stop` asks the host to end the child's process group: SIGTERM for a log
session and SIGHUP for a terminal session, then SIGKILL after `grace`
seconds; see [Stopping](#stopping). It returns once the session is gone. It
works when the host does not answer on its socket.

## Command line

`--run-dir DIR` goes before or after the command. Without it the run dir is
`$BURNER_DIR`, else `~/.burner`. `burner --version` prints the version, and
`burner COMMAND --help` shows each command with an example.

| Command | Prints | Exit status |
| --- | --- | --- |
| `burner start [--terminal] [--cwd DIR] [--log FILE] [--size COLSxROWS] NAME -- PROGRAM [ARG...]` | nothing | 0 |
| `burner ls [--json]` | one line per running session, sorted by name, no header | 0 |
| `burner alive NAME` | nothing | 0 running, 1 not running, 2 cannot tell or an error |
| `burner wait NAME [--timeout S]` | nothing | the child's status, 124 on timeout, 125 on an error |
| `burner stop NAME [--grace S]` | nothing | 0 |
| `burner screen NAME [-n N]` | the screen, or its last N rows; nothing for an empty screen | 0 |
| `burner tail NAME [-n N] [-f]` | the last N lines of the log; `-f` follows until the session ends; nothing when the name has no log | 0 |
| `burner press NAME KEY...` | nothing | 0 |
| `burner send NAME [--] TEXT` | nothing; types TEXT, then presses Enter | 0 |
| `burner attach NAME` | the session, on this terminal | 0 after a detach (Ctrl-\\), else the child's status; 125 on an error |
| `burner clean [NAME]` | the name of each session it cleaned, one per line | 0 |

- Unless the table gives another status, a command exits 1 on an error,
  2 on a usage error or a bad value such as an invalid name, size, or key,
  130 on Ctrl-C, and 141 when the reader of its output goes away, as in
  `burner tail -f web | head`.
  Each error is one line on stderr: `burner: MESSAGE`.
- The child's status is its exit code, or 128+N when signal N ended it.
- `start` needs `--` before the program. Everything after the first `--` goes
  to the program unchanged.
- `ls` prints NAME, PID, KIND (`terminal` or `log`), and STARTED (ISO 8601
  with the UTC offset), separated by tabs. `ls --json` prints a JSON list of
  objects with the keys `name`, `pid`, `kind`, and `started`.

## Files in the run dir

The run dir must be private: a directory you own, with no group or other
write bit, in a parent that other users cannot write unless it has the
sticky bit, as `/tmp` has. Every call checks this and raises
`UnsafeRunDirError`, a `HostError`, with the fix, such as `chmod go-w PATH`.
The run dir may be a symlink. The host creates a missing run dir, and any
missing parent, with mode 0700.

For a session `NAME`:

| File | Content |
| --- | --- |
| `NAME.lock` | Locked with `flock` by the host and by the child while the session runs. Removed when the session ends. |
| `NAME.json` | Meta, 0600: host pid and session id, child pid and process group, kind, size, log path, protocol version, start time, socket path. Removed when the session ends. |
| `NAME.sock` | The host's Unix socket, 0600. Removed when the session ends. |
| `NAME.log` | The log of a log session, 0600 when burner creates it. Before it passes 5 MiB, the host moves it to `NAME.log.1` and starts a new one. A terminal session has no log. |
| `NAME.log.1` | The log before the last move. |
| `NAME.exit` | How the child ended: `code` or `signal`, the time, the log path, and `error` when the host itself failed. Replaced once the next child of `NAME` has started. |

burner reads these files only when they are regular files that you own, and
never through a symlink. A meta file whose pids are not integers above 1
counts as missing, so a bad file never leads to a signal.

A socket path must fit in about 100 bytes. When `RUN_DIR/NAME.sock` is too
long, the host makes a dir of its own with `mkdtemp`, `/tmp/burner-XXXXXXXX`
with mode 0700, and records the socket path in the meta file. It holds a
shared `flock` on that dir, so systemd-tmpfiles leaves it alone, and removes
the socket and the dir when it exits.

A live host checks its files every 5 seconds. It makes its socket, socket
dir, or meta file again when one is missing, for example after a cleaner
removed old files from `/tmp`. When `NAME.lock` is gone or replaced, as
after the run dir was deleted, the name may belong to a new host, so the old
host stops its session and leaves the files at those paths alone. A deleted
run dir is noticed within 5 seconds.

## Lifecycle

| End | Files that remain | Processes that remain | How they go away |
| --- | --- | --- | --- |
| The child exits, `stop`, or SIGTERM to the host | `NAME.exit`; a log session also `NAME.log`, maybe `NAME.log.1`; a custom log | none in the child's group after the grace period | The next start of `NAME` removes `NAME.exit` once its child has started, and appends to the log. `burner clean [NAME]` removes all but a custom log. |
| A start that fails | none new; the exit record and log of the earlier run stay | none | |
| The host gets SIGKILL or crashes | lock, meta, socket (and its `/tmp` dir), log | the child while it lives; the name stays taken | `burner stop NAME`, then the next start or `burner clean` |
| The run dir is deleted | none in the run dir; the `/tmp` socket dir is removed | none after 5 s plus the grace period | |
| A reboot | lock, meta, socket, log, an older exit record | none | The next start of `NAME`, or `burner clean` |

`Host.clean(name=None)` and `burner clean [NAME]` remove the files of ended
sessions, or of one name, and return or print the names they cleaned. A run
dir should hold only burner's files, and `clean` acts only on proof that
burner made a file:

- `NAME.lock` counts when it is an empty regular file that you own.
- `NAME.json` counts when it holds a meta record: `protocol`, `socket`,
  and `pid`.
- `NAME.exit` counts when it holds an exit record: `time`, and `code` or
  `signal`.
- `NAME.log`, `NAME.log.1`, and `NAME.sock` go only for a name that a meta
  or exit record proves. An empty lock file alone is not enough.
- The socket dir under `/tmp` goes with the meta record that names it, and
  temp files go for a name that any of the three proves.
- Each file goes only when it is a regular file or socket that you own.

`clean` takes each name's lock first, so a running or starting session
keeps its files. Naming a running session, or a name whose `NAME.lock` is
not burner's, raises `HostError`; without a name such a session is skipped.
A log chosen with `log=`, any file without proof, and the run dir itself
always stay.

## Stopping

`stop` and the end of the child run one ladder of signals on the child's
process group:

- A log session gets SIGTERM, then SIGKILL after the grace period.
- A terminal session gets SIGHUP, as from a closing terminal, then SIGTERM
  after half the grace period, then SIGKILL after the full grace period.

The grace period is 5 seconds unless `stop` gives another. A second `stop`
with a shorter grace period moves the deadlines earlier. When the host does
not answer, `stop` sends the same signals from the client.

When the child exits, the host writes `NAME.exit`, tells an attached client,
removes its files, and frees the name at once. It keeps the child unreaped,
so no new process can take its process group id, and runs the ladder on the
rest of the group. The ladder starts at once unless a stop was already under
way. The host exits as soon as the group is empty, or after SIGKILL. So a
process in the child's group outlives the session by at most the grace
period. Output it writes after the child exited is not logged: it gets
EPIPE, so a new session of the same name starts with a log of its own. A
process that starts its own process group or session is out of reach.

## Liveness

A session is alive while `NAME.lock` is locked. `alive` is true when another
process holds the lock. Probes take a shared lock, so two probes never see
each other as a host. `gone` is true only when the lock is known to be free.
When the check itself fails, both return false, so neither is a guess. Every
locker checks after `flock` that the file at the path is still the one it
locked, because an ending host unlinks the file before it unlocks it.

The child inherits the host's lock, so the name stays taken while the child
lives, even after the host dies. At a normal end the host unlocks the file,
so a process that kept the lock fd does not keep the name. After the host
dies, `stop` reaches the child with signals. The client signals the child's
process group only while the child leads it or the group still has
members, and sends SIGKILL to the host pid only while that pid is still in
the host's session. A process that left the group and kept the lock fd
holds the name until it exits or you delete `NAME.lock`, and `stop` reports
it. A log child that writes after its host died gets EPIPE or SIGPIPE.

## Protocol and versions

One connection per call on the session's Unix socket. Each frame is a type
byte, a 4-byte big-endian length, and a payload. `J` frames carry a JSON
object with a `v` field; `D` frames carry raw terminal bytes during attach.
A request may be up to 1 MiB and a reply up to 16 MiB, so a whole screen
fits. The start request (argv, environment, cwd) goes to the host over a
pipe, so it never shows up on a command line or on disk. When the host
cannot write its ready report on that pipe, it undoes the start.
`src/burner/protocol.py` lists the operations.

No request ends a session: a request the host cannot handle gets an error
reply, and a client that breaks the protocol is dropped with one line on the
host's stderr.

A host keeps running the code it started with, so after an upgrade a client
may talk to an older host. The frame format never changes. Operations and
fields are only added, and meta keys and file suffixes only gain entries. A
host answers an operation it does not know with an error and keeps the
connection open, and both sides ignore fields they do not know.

## Limits and non-goals

- No windows, panes, or scrollback. The screen is the visible screen only.
- One attached client at a time. A new attach detaches the old one.
- A terminal is 1 to 1000 cells on each side and at most 100,000 cells in
  all. `start` refuses a larger size. Attach and resize use the largest
  size within the limits, so a large terminal still attaches.
- The host holds at most 4 MB of input the program has not read yet. Past
  that, `write` raises `HostError` and attach drops what was typed.
- The host reads the PTY 4 KiB at a time, so one screen update stays short
  whatever the program writes. A cell holds at most 16 code points, enough
  for a long emoji sequence; combining marks past that are dropped. An
  error in the screen model resets the screen and keeps the session.
- Attach paints the screen as plain text, without colors. It turns on the
  input modes the session has on (cursor keys, mouse reporting, focus
  events, bracketed paste), so keys, pastes, and clicks reach the program in
  the form it asked for. Detach, SIGTERM, and SIGHUP turn them off again and
  restore the terminal.
- While no client is attached, the host answers cursor position, status, and
  device attribute queries for the child, as long as the child has less
  than 64 KiB of input it has not read. Other queries go unanswered.
- A log session takes at most 10 MiB of disk: `NAME.log` and `NAME.log.1`
  of 5 MiB each. A host error goes to the log of a log session, and a fatal
  one also to `NAME.exit` as `error`.
- At rest a host wakes only for the file check every 5 seconds and, while a
  client is connected, to drop an idle one.
- `tools/bench.py` measures start and call times, memory, disk, wakeups,
  and CPU against their budgets.
- After a host was killed, `stop` signals the recorded process group when a
  session leader has that id or the group still has members. A new process
  that reused that id in the meantime could in theory get the signal.
- POSIX only for now (macOS and Linux).

See `SECURITY.md` for the trust model and how to report a vulnerability.
