"""Keep named processes running after their starter exits.

Each session has its own small host process. Clients read its screen or
log, send it input, attach a terminal, and stop it, by name.
"""

__version__ = "0.2.0"

from .client import ExitStatus, Host, Session
from .errors import AlreadyRunningError, HostError, NotRunningError, UnsafeRunDirError
from .keys import Key

__all__ = [
    "AlreadyRunningError",
    "ExitStatus",
    "Host",
    "HostError",
    "Key",
    "NotRunningError",
    "Session",
    "UnsafeRunDirError",
]
