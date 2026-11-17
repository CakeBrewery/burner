"""Errors raised by burner."""


class HostError(RuntimeError):
    """A session operation failed."""


class NotRunningError(HostError):
    """The named session is not running."""


class AlreadyRunningError(HostError):
    """The session name is taken by a running session."""


class UnsafeRunDirError(HostError):
    """The run dir is not private to this user."""
