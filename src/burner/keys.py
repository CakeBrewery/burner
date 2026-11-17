"""Key names and the bytes a terminal sends for them."""

from __future__ import annotations

from enum import StrEnum


class Key(StrEnum):
    """Named keys for ``Host.press``. Plain strings such as ``"Up"`` work too."""

    UP = "Up"
    DOWN = "Down"
    LEFT = "Left"
    RIGHT = "Right"
    ENTER = "Enter"
    ESCAPE = "Escape"
    TAB = "Tab"
    BACK_TAB = "BackTab"
    BACKSPACE = "Backspace"
    SPACE = "Space"
    HOME = "Home"
    END = "End"
    PAGE_UP = "PageUp"
    PAGE_DOWN = "PageDown"
    INSERT = "Insert"
    DELETE = "Delete"
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"
    F7 = "F7"
    F8 = "F8"
    F9 = "F9"
    F10 = "F10"
    F11 = "F11"
    F12 = "F12"


# Keys whose escape sequence uses SS3 (ESC O) instead of CSI (ESC [) when the
# application has turned on application cursor keys (DECCKM).
_CURSOR = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}

_FIXED = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "backtab": b"\x1b[Z",
    "backspace": b"\x7f",
    "space": b" ",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}


def encode(name: str, application_cursor: bool = False) -> bytes:
    """Return the bytes for one key name, matched without regard to case.

    ``C-a`` to ``C-z`` are control keys. Raises ValueError for an unknown name.
    """
    key = str(name).lower()
    if key in _CURSOR:
        return (b"\x1bO" if application_cursor else b"\x1b[") + _CURSOR[key].encode()
    if key in _FIXED:
        return _FIXED[key]
    if len(key) == 3 and key.startswith("c-") and "a" <= key[2] <= "z":
        return bytes([ord(key[2]) - ord("a") + 1])
    raise ValueError(f"unknown key {str(name)!r} (see burner press --help)")
