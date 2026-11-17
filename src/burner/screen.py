"""A terminal screen model that turns the output of a program into rows of plain text.

It covers the VT and xterm subset that full screen programs such as Ink apps, vim,
less, and top use. Colors and other attributes are parsed and dropped. The rows
match what `tmux capture-pane -p` prints for the same output, including tmux's
handling of a pending wrap: after a character lands in the last column the cursor
column equals `cols`, the next printable character wraps first, and line feeds,
VPA, and edits keep that state as tmux does.
"""

from __future__ import annotations

import codecs
import re
import unicodedata
from functools import lru_cache

PAD = ""  # The second cell of a wide character. It joins to nothing.
ZWJ = "\u200d"
VS16 = "\ufe0f"  # Emoji presentation: tmux draws the character before it two cells wide.
_MAX_PARAM = 65535
_MAX_SEQ = 256  # Longer CSI parameter strings are cut, not stored.
_MAX_CELL = 16  # Combining marks past this length on one cell are dropped.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# DEC private modes that change what the terminal sends: cursor keys (1), mouse
# reporting (1000, 1002, 1003) and its encodings (1005, 1006, 1015), focus
# events (1004), and bracketed paste (2004). Programs set them once, so a
# terminal that attaches later needs them set again to send the same input.
INPUT_MODES = (1, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 2004)

GROUND, ESCAPE, ESCAPE_INTER, CSI, STRING = range(5)


@lru_cache(maxsize=8192)
def char_width(ch: str) -> int:
    """Return the cells a character takes: 0 for marks that attach, 2 for wide ones."""
    o = ord(ch)
    if o == 0xAD:  # A soft hyphen shows as one cell.
        return 1
    if 0x1160 <= o <= 0x11FF or 0xD7B0 <= o <= 0xD7FF:  # Hangul vowels and finals join.
        return 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _cell_width(cell: str) -> int:
    return 2 if cell and (char_width(cell[0]) == 2 or VS16 in cell) else 1


def _is_skin_tone(ch: str) -> bool:
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


class Screen:
    """The visible screen of one terminal, fed with the bytes a program writes."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._state = GROUND
        self._seq: list[str] = []
        self._string_kind = ""
        self._reset()

    def _reset(self) -> None:
        self._grid = self._blank_grid()
        self._main: list[list[str]] | None = None  # The main grid while the alternate is shown.
        self._x = 0  # Equals cols while a wrap is pending.
        self._y = 0
        self._top = 0
        self._bottom = self.rows - 1
        self._autowrap = True
        self._origin = False
        self._insert = False
        self._modes: set[int] = set()
        self._saved = (0, 0, False)
        self._saved_alt = (0, 0, False)
        self._tabs = set(range(8, self.cols, 8))
        self._last = " "

    # Public interface.

    @property
    def cursor(self) -> tuple[int, int]:
        """The (row, column) of the cursor; a pending wrap reports the last column."""
        return (self._y, min(self._x, self.cols - 1))

    @property
    def application_cursor_keys(self) -> bool:
        return 1 in self._modes

    @property
    def bracketed_paste(self) -> bool:
        return 2004 in self._modes

    @property
    def input_modes(self) -> list[int]:
        """The modes from INPUT_MODES that are on, in that order."""
        return [m for m in INPUT_MODES if m in self._modes]

    @property
    def alternate(self) -> bool:
        return self._main is not None

    def lines(self) -> list[str]:
        """Every row of the visible screen, top to bottom, right-trimmed."""
        return ["".join(row).rstrip(" ") for row in self._grid]

    def text(self, last: int | None = None) -> str:
        """Rows joined by newlines, trailing blank rows dropped; `last` keeps the final rows."""
        return "\n".join(self.text_rows(last))

    def text_rows(self, last: int | None = None) -> list[str]:
        """The rows that `text` joins."""
        rows = self.lines()
        while rows and not rows[-1]:
            rows.pop()
        if last is not None:
            rows = rows[-last:] if last > 0 else []
        return rows

    def replay(self) -> bytes:
        """Bytes that set the input modes and repaint this screen as plain text.

        They suit a fresh terminal of the same size.
        """
        out = [f"\x1b[?{m}h" for m in self.input_modes]
        out.append("\x1b[H\x1b[2J")
        for i, line in enumerate(self.lines()):
            if line:
                out.append(f"\x1b[{i + 1};1H{line}")
        if self._x < self.cols:
            out.append(f"\x1b[{self._y + 1};{self._x + 1}H")
        else:
            # Write the last cell again so the next character wraps, as it does here.
            row, x = self._grid[self._y], self.cols - 1
            if row[x] == PAD and x > 0:
                x -= 1
            out.append(f"\x1b[{self._y + 1};{x + 1}H{row[x]}")
        return "".join(out).encode("utf-8")

    def resize(self, cols: int, rows: int) -> None:
        """Change the size. Content stays at the top left and is not reflowed."""
        cols, rows = max(1, cols), max(1, rows)
        for grid in [self._grid] + ([self._main] if self._main is not None else []):
            for row in grid:
                if len(row) > cols:
                    del row[cols:]
                    _repair(row, cols - 1, cols)
                else:
                    row.extend(" " * (cols - len(row)))
            del grid[rows:]
            grid.extend([" "] * cols for _ in range(rows - len(grid)))
        self.cols, self.rows = cols, rows
        self._x = min(self._x, cols - 1)
        self._y = min(self._y, rows - 1)
        self._top, self._bottom = 0, rows - 1
        self._tabs = set(range(8, cols, 8))

    def feed(self, data: bytes) -> None:
        """Consume output bytes. Sequences split across calls are joined. Never raises."""
        text = self._decoder.decode(bytes(data))
        i, n = 0, len(text)
        while i < n:
            if self._state == GROUND:
                m = _CONTROL.search(text, i)
                j = m.start() if m else n
                run = text[i:j]
                if run.isascii() and self._autowrap:
                    self._print_ascii(run)
                else:
                    for ch in run:
                        self._print(ch)
                if j < n:
                    self._control(text[j])
                i = j + 1
                continue
            ch = text[i]
            i += 1
            if self._state == ESCAPE:
                self._on_escape(ch)
            elif self._state == ESCAPE_INTER:
                self._on_escape_inter(ch)
            elif self._state == CSI:
                self._on_csi(ch)
            else:
                self._on_string(ch)

    # Parser states.

    def _control(self, ch: str) -> None:
        """Run a C0 control. C1 code points are ignored."""
        c = ord(ch)
        if c == 0x1B:
            self._state = ESCAPE
        elif c == 0x0D:
            self._x = 0
        elif c in (0x0A, 0x0B, 0x0C):
            self._linefeed()
        elif c == 0x08:
            self._x = max(0, self._x - 1)
        elif c == 0x09:
            self._tab(1)
        elif c in (0x18, 0x1A):
            self._state = GROUND

    def _on_escape(self, ch: str) -> None:
        self._state = GROUND
        if ch == "[":
            self._state, self._seq = CSI, []
        elif ch in "]PX^_":
            self._state, self._string_kind = STRING, ch
        elif " " <= ch <= "/":
            self._state, self._seq = ESCAPE_INTER, [ch]
        elif ord(ch) < 0x20:
            self._control(ch)
            if ch not in "\x18\x1a\x1b":
                self._state = ESCAPE
        else:
            self._esc_dispatch(ch)

    def _on_escape_inter(self, ch: str) -> None:
        if " " <= ch <= "/":
            self._seq.append(ch)
        elif "0" <= ch <= "~":
            self._state = GROUND
            if self._seq == ["#"] and ch == "8":
                self._grid = [["E"] * self.cols for _ in range(self.rows)]
        elif ord(ch) < 0x20:
            self._control(ch)
        else:
            self._state = GROUND

    def _on_csi(self, ch: str) -> None:
        o = ord(ch)
        if 0x40 <= o <= 0x7E:
            self._state = GROUND
            self._csi_dispatch("".join(self._seq), ch)
        elif 0x20 <= o <= 0x3F:
            if len(self._seq) < _MAX_SEQ:
                self._seq.append(ch)
        elif o < 0x20:
            self._control(ch)

    def _on_string(self, ch: str) -> None:
        # OSC, DCS, SOS, PM, and APC end with ST (ESC \); OSC also ends with BEL.
        if ch == "\x1b":
            self._state = ESCAPE
        elif ch in "\x18\x1a" or (ch == "\x07" and self._string_kind == "]"):
            self._state = GROUND

    def _esc_dispatch(self, ch: str) -> None:
        if ch == "7":
            self._saved = (self._y, self._x, self._origin)
        elif ch == "8":
            self._restore(self._saved)
        elif ch == "D":
            self._linefeed()
        elif ch == "E":
            self._linefeed()
            self._x = 0
        elif ch == "M":
            self._reverse_index()
        elif ch == "H" and self._x < self.cols:
            self._tabs.add(self._x)
        elif ch == "c":
            self._reset()

    def _csi_dispatch(self, seq: str, final: str) -> None:
        marker = ""
        if seq and seq[0] in "<=>?":
            marker, seq = seq[0], seq[1:]
        if any(c < "0" for c in seq):
            return  # Sequences with intermediates (cursor style, soft reset) change no text.
        args = []
        for part in seq.split(";"):
            head = part.split(":", 1)[0]
            args.append(min(int(head), _MAX_PARAM) if head.isdigit() else 0)
        if marker == "?" and final in "hl":
            for mode in args:
                self._set_private(mode, final == "h")
        elif not marker:
            handler = _CSI.get(final)
            if handler is not None:
                n = args[0] or 1
                handler(self, n, args)
        # Other private sequences are queries or kitty keyboard requests: no screen change.

    # Printing.

    def _print(self, ch: str) -> None:
        w = char_width(ch)
        if w == 0:
            self._combine(ch)
            return
        if w > self.cols or (w == 2 and self._join(ch)):
            return
        if self._x + w > self.cols:
            self._wrap(w)
        self._put([ch] if w == 1 else [ch, PAD])
        self._last = ch
        self._x = self._x + w if self._autowrap else min(self._x + w, self.cols - 1)

    def _scroll_full_rows(self, cells: list[str], count: int) -> None:
        """From a pending wrap on the bottom margin, print count rows full of cells.

        Each row scrolls in blank and fills up, so the region ends with up to
        count copies of one row at its bottom.
        """
        if self._x < self.cols:
            self._erase(self._y, self._x, self.cols)
        per_row = self.cols // len(cells)
        full = cells * per_row + [" "] * (self.cols - per_row * len(cells))
        count = min(count, self._bottom - self._top + 1)
        del self._grid[self._top : self._top + count]
        at = self._bottom + 1 - count
        self._grid[at:at] = [full.copy() for _ in range(count)]
        self._x = per_row * len(cells)

    def _wrap(self, w: int) -> None:
        """Make room for a character w cells wide that does not fit after the cursor."""
        if self._autowrap:
            if self._x < self.cols:
                self._erase(self._y, self._x, self.cols)
            self._linefeed()
            self._x = 0
        else:
            self._x = self.cols - w

    def _put(self, cells: list[str]) -> None:
        """Write cells at the cursor, or insert them in insert mode. They fit in the row."""
        x, row, n = self._x, self._grid[self._y], len(cells)
        if self._insert:
            row[x:x] = cells
            del row[self.cols :]
            _repair(row, self.cols - 1, self.cols)
        else:
            row[x : x + n] = cells
        _repair(row, x - 1, x + 1)
        _repair(row, x + n - 1, x + n + 1)

    def _print_ascii(self, run: str) -> None:
        """Print narrow characters a row at a time; the result equals `_print` per character."""
        while run:
            if self._x >= self.cols:
                self._wrap(1)
            n = min(len(run), self.cols - self._x)
            self._put(list(run[:n]))
            self._x += n
            self._last, run = run[n - 1], run[n:]

    def _repeat(self, n: int) -> None:
        """Print the last character n more times, as `_print` would one at a time."""
        ch = self._last
        w = char_width(ch)
        if w > self.cols:
            return
        n = min(n, self.cols * self.rows)
        if w == 1:
            self._print_copies([ch], n)
            return
        n -= self._join_copies(ch, n)
        if _is_skin_tone(ch):
            # Each new copy takes the copies after it into its cell until the cell is full.
            full, rest = divmod(n, _MAX_CELL)
            self._print_copies([ch * _MAX_CELL, PAD], full)
            self._print_copies([ch * rest, PAD], 1 if rest else 0)
        else:
            self._print_copies([ch, PAD], n)

    def _print_copies(self, cells: list[str], n: int) -> None:
        """Print n copies of one character's cells, a row at a time."""
        w = len(cells)
        if not self._autowrap:
            while n:
                if self._x + w > self.cols:
                    self._wrap(w)
                k = min(n, (self.cols - self._x) // w)
                self._put(cells * k)
                self._x = min(self._x + k * w, self.cols - 1)
                # Past the margin every copy lands on the same cells, so one more is enough.
                n = min(n - k, 1)
            return
        per_row = self.cols // w
        while n:
            if self._x + w > self.cols:
                if self._y == self._bottom and n >= per_row:
                    self._scroll_full_rows(cells, n // per_row)
                    n %= per_row
                    continue
                self._wrap(w)
            k = min(n, (self.cols - self._x) // w)
            self._put(cells * k)
            self._x += k * w
            n -= k

    def _previous_cell(self) -> int:
        """The column of the character before the cursor, or -1."""
        x, row = self._x - 1, self._grid[self._y]
        return x - 1 if x > 0 and row[x] == PAD else x

    def _combine(self, ch: str) -> None:
        x, row = self._previous_cell(), self._grid[self._y]
        if x < 0 or len(row[x]) >= _MAX_CELL:
            return
        if ch == VS16 and _cell_width(row[x]) == 1:
            if x + 1 != self._x or self._x >= self.cols:
                return  # No room to widen it.
            row[x] += ch
            row[x + 1] = PAD
            _repair(row, x + 2, x + 3)
            self._x += 1
        else:
            row[x] += ch

    def _join_copies(self, ch: str, n: int) -> int:
        """Join up to n copies of a wide character to the emoji before the cursor.

        Returns how many joined. Only a skin tone joins more than once.
        """
        if not n or not self._join(ch):
            return 0
        if not _is_skin_tone(ch):
            return 1
        x, row = self._previous_cell(), self._grid[self._y]
        more = min(n - 1, _MAX_CELL - len(row[x]))
        row[x] += ch * more
        return 1 + more

    def _join(self, ch: str) -> bool:
        """Add a wide character to the emoji before it after a joiner or as a skin tone."""
        x, row = self._previous_cell(), self._grid[self._y]
        if x < 0 or _cell_width(row[x]) != 2 or len(row[x]) >= _MAX_CELL:
            return False
        if not (row[x].endswith(ZWJ) or _is_skin_tone(ch)):
            return False
        row[x] += ch
        return True

    # Cursor movement and scrolling.

    def _linefeed(self) -> None:
        if self._y == self._bottom:
            self._scroll_up(self._top, self._bottom, 1)
        elif self._y < self.rows - 1:
            self._y += 1

    def _reverse_index(self) -> None:
        if self._y == self._top:
            self._scroll_down(self._top, self._bottom, 1)
        elif self._y > 0:
            self._y -= 1

    def _scroll_up(self, top: int, bottom: int, n: int) -> None:
        n = min(n, bottom - top + 1)
        del self._grid[top : top + n]
        self._grid[bottom + 1 - n : bottom + 1 - n] = [[" "] * self.cols for _ in range(n)]

    def _scroll_down(self, top: int, bottom: int, n: int) -> None:
        n = min(n, bottom - top + 1)
        del self._grid[bottom + 1 - n : bottom + 1]
        self._grid[top:top] = [[" "] * self.cols for _ in range(n)]

    def _goto_row(self, y: int) -> None:
        """Move to a row counted from the origin, clamped. The column stays as it is."""
        lo, hi = (self._top, self._bottom) if self._origin else (0, self.rows - 1)
        self._y = min(max(y + lo, lo), hi)

    def _set_col(self, x: int) -> None:
        self._x = min(max(x, 0), self.cols - 1)

    def _move_rows(self, n: int) -> None:
        """Move up (n < 0) or down, stopping at a margin when the cursor is inside it."""
        top = self._top if self._y >= self._top else 0
        bottom = self._bottom if self._y <= self._bottom else self.rows - 1
        self._y = min(max(self._y + n, top), bottom)
        self._set_col(self._x)

    def _tab(self, n: int) -> None:
        if self._x >= self.cols:
            return
        stops = sorted(t for t in self._tabs if t > self._x)
        self._x = stops[n - 1] if n <= len(stops) else self.cols - 1

    def _back_tab(self, n: int) -> None:
        stops = sorted((t for t in self._tabs if t < self._x), reverse=True)
        self._x = stops[n - 1] if n <= len(stops) else 0

    def _restore(self, saved: tuple[int, int, bool]) -> None:
        y, x, self._origin = saved
        self._y = min(y, self.rows - 1)
        self._set_col(x)

    # Erasing and editing.

    def _erase(self, y: int, x0: int, x1: int) -> None:
        row = self._grid[y]
        x0, x1 = max(x0, 0), min(x1, self.cols)
        if x0 < x1:
            row[x0:x1] = [" "] * (x1 - x0)
            # Only a wide character cut at either edge needs repair.
            _repair(row, x0 - 1, x0 + 1)
            _repair(row, x1 - 1, x1 + 1)

    def _erase_rows(self, y0: int, y1: int) -> None:
        self._grid[y0:y1] = [[" "] * self.cols for _ in range(y0, y1)]

    def _erase_display(self, mode: int) -> None:
        if mode == 2:
            self._erase_rows(0, self.rows)
        elif mode == 0:
            self._erase(self._y, self._x, self.cols)
            self._erase_rows(self._y + 1, self.rows)
        elif mode == 1:
            self._erase_rows(0, self._y)
            self._erase(self._y, 0, self._x + 1)

    def _erase_line(self, mode: int) -> None:
        if mode in (0, 2):
            self._erase(self._y, 0 if mode else self._x, self.cols)
        if mode in (1, 2):
            self._erase(self._y, 0, self._x + 1)

    def _edit_chars(self, n: int, insert: bool) -> None:
        row, x = self._grid[self._y], self._x
        n = min(n, self.cols - x)
        if n <= 0:
            return
        if insert:
            row[x:x] = [" "] * n
            del row[self.cols :]
        else:
            del row[x : x + n]
            row.extend([" "] * n)
        _repair(row, 0, self.cols)

    def _edit_lines(self, n: int, insert: bool) -> None:
        if self._top <= self._y <= self._bottom:
            if insert:
                self._scroll_down(self._y, self._bottom, n)
            else:
                self._scroll_up(self._y, self._bottom, n)

    def _set_region(self, args: list[int]) -> None:
        top = (args[0] or 1) - 1
        bottom = min((args[1] if len(args) > 1 else 0) or self.rows, self.rows) - 1
        if top < bottom:
            self._top, self._bottom = top, bottom
            self._goto_row(0)
            self._x = 0

    # Modes.

    def _set_private(self, mode: int, on: bool) -> None:
        if mode in INPUT_MODES:
            if on:
                self._modes.add(mode)
            else:
                self._modes.discard(mode)
        elif mode == 6:
            self._origin = on
            self._goto_row(0)
            self._x = 0
        elif mode == 7:
            self._autowrap = on
            self._set_col(self._x)
        elif mode in (47, 1047, 1049):
            if on and self._main is None:
                if mode == 1049:
                    self._saved_alt = (self._y, self._x, self._origin)
                self._main, self._grid = self._grid, self._blank_grid()
            elif not on and self._main is not None:
                self._grid, self._main = self._main, None
                if mode == 1049:
                    self._restore(self._saved_alt)

    def _blank_grid(self) -> list[list[str]]:
        return [[" "] * self.cols for _ in range(self.rows)]


def _repair(row: list[str], lo: int, hi: int) -> None:
    """Blank any half of a wide character whose other half was overwritten."""
    n = len(row)
    for i in range(max(lo, 0), min(hi, n)):
        c = row[i]
        if c == PAD:
            if i == 0 or row[i - 1] == PAD or _cell_width(row[i - 1]) != 2:
                row[i] = " "
        elif _cell_width(c) == 2 and (i + 1 >= n or row[i + 1] != PAD):
            row[i] = " "


def _cup(s: Screen, args: list[int]) -> None:
    col = args[1] if len(args) > 1 else 0
    s._goto_row((args[0] or 1) - 1)
    s._set_col((col or 1) - 1)


def _clear_tabs(s: Screen, mode: int) -> None:
    if mode == 0:
        s._tabs.discard(s._x)
    elif mode == 3:
        s._tabs.clear()


def _ansi_mode(s: Screen, args: list[int], on: bool) -> None:
    if 4 in args:
        s._insert = on


# Each handler takes the screen, the first parameter with 0 read as 1, and all parameters.
_CSI = {
    "A": lambda s, n, a: s._move_rows(-n),
    "B": lambda s, n, a: s._move_rows(n),
    "e": lambda s, n, a: s._move_rows(n),
    "C": lambda s, n, a: s._set_col(s._x + n),
    "a": lambda s, n, a: s._set_col(s._x + n),
    "D": lambda s, n, a: s._set_col(s._x - n),
    "E": lambda s, n, a: (s._move_rows(n), s._set_col(0)),
    "F": lambda s, n, a: (s._move_rows(-n), s._set_col(0)),
    "G": lambda s, n, a: s._set_col(n - 1),
    "`": lambda s, n, a: s._set_col(n - 1),
    "H": lambda s, n, a: _cup(s, a),
    "f": lambda s, n, a: _cup(s, a),
    "d": lambda s, n, a: s._goto_row(n - 1),
    "J": lambda s, n, a: s._erase_display(a[0]),
    "K": lambda s, n, a: s._erase_line(a[0]),
    "L": lambda s, n, a: s._edit_lines(n, True),
    "M": lambda s, n, a: s._edit_lines(n, False),
    "@": lambda s, n, a: s._edit_chars(n, True),
    "P": lambda s, n, a: s._edit_chars(n, False),
    "X": lambda s, n, a: s._erase(s._y, s._x, s._x + n),
    "S": lambda s, n, a: s._scroll_up(s._top, s._bottom, n),
    "T": lambda s, n, a: len(a) == 1 and s._scroll_down(s._top, s._bottom, n),
    "I": lambda s, n, a: s._tab(n),
    "Z": lambda s, n, a: s._back_tab(n),
    "b": lambda s, n, a: s._repeat(n),
    "g": lambda s, n, a: _clear_tabs(s, a[0]),
    "r": lambda s, n, a: s._set_region(a),
    "s": lambda s, n, a: setattr(s, "_saved", (s._y, s._x, s._origin)),
    "u": lambda s, n, a: s._restore(s._saved),
    "h": lambda s, n, a: _ansi_mode(s, a, True),
    "l": lambda s, n, a: _ansi_mode(s, a, False),
}
