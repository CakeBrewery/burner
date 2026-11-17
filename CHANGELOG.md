# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-17

First public release.

### Added

- Named sessions, each with its own host process. A session is alive while
  its lock is held, so no server holds every session.
- Terminal sessions: the child runs on a PTY, and the host keeps a screen
  model. `Host.screen` reads the screen as text, `press`, `send`, and
  `write` type into it, and `attach` connects a terminal to it.
- Log sessions: the child's output goes to a log file that the host rotates
  at 5 MiB. `Host.tail` reads its end.
- `Host.start`, `alive`, `gone`, `sessions`, `stop`, `wait`, `exit_status`,
  `clean`, and `log_path`, with the typed records `Session` and
  `ExitStatus`.
- Errors `HostError`, `NotRunningError`, `AlreadyRunningError`, and
  `UnsafeRunDirError`.
- The `burner` command: `start`, `ls`, `alive`, `wait`, `stop`, `screen`,
  `tail`, `press`, `send`, `attach`, and `clean`, with fixed exit statuses
  and one-line errors.
- Type information (`py.typed`).
- `SECURITY.md` with the trust model and how to report a vulnerability.
- `tools/bench.py` measures what a session costs against budgets, and
  `tools/record.py` records a program's output next to tmux's rendering of
  it.

### Security

- Every call refuses a run dir that other users can change.
- burner reads its own files only when they are regular files that the user
  owns, never through a symlink. `clean` removes only files that burner
  provably made.
- No client request ends a session, and a child's output cannot stall its
  host or grow it without limit.
