# Security

## Trust model

burner trusts the user who owns the run dir, and root. It does not trust
other users, the output of the child, or the content of files found in the
run dir.

- **Other users.** The run dir must be private: owned by you, with no group
  or other write bit, in a parent that only you can write or that has the
  sticky bit. Every call checks this and refuses an unsafe run dir. Sockets
  have mode 0600. A socket dir under `/tmp` is made with `mkdtemp` for one
  host, so no other user can create or predict it.
- **File content.** burner reads its files only when they are regular files
  that you own, never through a symlink, and accepts a meta file only when
  its pids are integers above 1. `clean` removes a file only on proof that burner made
  it: an empty lock file, a meta record, or an exit record of this user,
  and the logs and socket of a name only when a meta or exit record proves
  the name. It
  never removes a log chosen with `log=`.
- **Child output.** A program on a terminal cannot stall its host for long
  or grow it without limit: reads are short, query answers stop while the
  program does not read them, the screen has a size limit, and errors in the
  screen model reset it. A log session takes at most twice the log limit on
  disk.
- **Clients.** No request ends a session. A request the host cannot handle
  gets an error reply, and a client that breaks the protocol is dropped.
- **Signals.** burner signals only the processes that its meta file names,
  and only while the session's lock is held. While the host lives, that
  shows they still belong to the session. After the host was killed, the
  child may hold the lock alone. `stop` then signals the recorded process
  group when a session leader has that id or the group still has members,
  and kills the recorded host pid only while it is in the recorded session.
  If the pid were reused by a new session leader in that window, that
  process could in theory get the signal.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.2.x | yes |

## Reporting a vulnerability

Report it privately through GitHub: open the repository's Security tab and
choose "Report a vulnerability". Do not open a public issue. Include the
version, the platform, and the steps that show the problem. The report and
its fix are discussed in the private advisory until a release fixes it.
