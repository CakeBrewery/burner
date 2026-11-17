"""Tests for the terminal screen model."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from burner.screen import Screen

FIXTURES = Path(__file__).parent / "fixtures"
X80 = b"x" * 80


def screen(data: bytes | str, cols: int = 20, rows: int = 5) -> Screen:
    s = Screen(cols, rows)
    s.feed(data.encode() if isinstance(data, str) else data)
    return s


def rows(data: bytes | str, cols: int = 20, rows: int = 5) -> list[str]:
    return screen(data, cols, rows).lines()


# Text, wrapping, and the interface shape.


def test_blank_screen():
    s = Screen()
    assert s.lines() == [""] * 24
    assert s.text() == ""
    assert s.cursor == (0, 0)
    assert not s.alternate and not s.bracketed_paste and not s.application_cursor_keys


def test_plain_text_and_newlines():
    assert rows("ab\r\ncd")[:2] == ["ab", "cd"]


def test_line_feed_keeps_the_column():
    assert rows("ab\ncd")[:2] == ["ab", "  cd"]


def test_vertical_tab_and_form_feed_move_down():
    assert rows("a\x0bb\x0cc")[:3] == ["a", " b", "  c"]


def test_autowrap_is_deferred_at_the_last_column():
    s = screen("abcde", cols=5)
    assert s.cursor == (0, 4)
    assert s.lines()[:2] == ["abcde", ""]
    s.feed(b"f")
    assert s.lines()[:2] == ["abcde", "f"]
    assert s.cursor == (1, 1)


def test_carriage_return_cancels_a_pending_wrap():
    assert rows("abcde\rX", cols=5)[:2] == ["Xbcde", ""]


def test_line_feed_keeps_a_pending_wrap_like_tmux():
    # tmux keeps the cursor past the last column, so the next character wraps again.
    assert rows("abcde\nX", cols=5)[:3] == ["abcde", "", "X"]


def test_vpa_keeps_a_pending_wrap_like_tmux():
    assert rows("abcde\x1b[3dX", cols=5)[:4] == ["abcde", "", "", "X"]


def test_edits_at_a_pending_wrap_change_nothing():
    for seq in ("\x1b[K", "\x1b[X", "\x1b[P", "\x1b[@", "\x1b[J"):
        assert rows("abcde" + seq + "Z", cols=5)[:2] == ["abcde", "Z"], seq


def test_cursor_left_from_a_pending_wrap_counts_from_past_the_edge():
    assert rows("abcde\x1b[DY", cols=5)[0] == "abcdY"
    assert rows("abcde\x1b[2DY", cols=5)[0] == "abcYe"
    assert rows("abcde\bY", cols=5)[0] == "abcdY"


def test_autowrap_off_overwrites_the_last_column():
    assert rows("\x1b[?7labcdefg", cols=5)[:2] == ["abcdg", ""]
    assert rows("\x1b[?7labcdefg\x1b[?7hhi", cols=5)[:2] == ["abcdh", "i"]


def test_text_drops_trailing_blank_rows_and_keeps_the_last_rows():
    s = screen("a\r\n\r\nb\r\nc\r\n")
    assert s.text() == "a\n\nb\nc"
    assert s.text(last=2) == "b\nc"
    assert s.text(last=0) == ""
    assert s.text(last=99) == "a\n\nb\nc"


def test_lines_are_right_trimmed_and_count_every_row():
    s = screen("a   \x1b[3;5Hb  ")
    assert s.lines() == ["a", "", "    b", "", ""]


# Wide characters and joining marks.


def test_wide_characters_take_two_cells():
    s = screen("中a")
    assert s.lines()[0] == "中a"
    assert s.cursor == (0, 3)
    assert rows("中\x1b[1;3HX")[0] == "中X"


def test_wide_character_at_the_last_column_wraps_first():
    assert rows("abcd中", cols=5)[:2] == ["abcd", "中"]


def test_overwriting_half_of_a_wide_character_blanks_the_other_half():
    assert rows("中文\x1b[1;2HX")[0] == " X文"
    assert rows("中文\x1b[1;1HX")[0] == "X 文"
    assert rows("中文\x1b[1;1H\x1b[P")[0] == " 文"


def test_combining_marks_attach_to_the_previous_cell():
    s = screen("e\u0301x")
    assert s.lines()[0] == "e\u0301x"
    assert s.cursor == (0, 2)


def test_combining_mark_after_a_pending_wrap_attaches_to_the_last_cell():
    assert rows("abcde\u0301f", cols=5)[:2] == ["abcde\u0301", "f"]


def test_variation_selector_16_widens_the_character():
    assert rows("\u26a0\ufe0f\x1b[1;3HY")[0] == "\u26a0\ufe0fY"
    assert rows("\u26a0\x1b[1;3HY")[0] == "\u26a0 Y"


def test_zero_width_joiner_sequences_and_skin_tones_share_one_cell():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert rows(family + "\x1b[1;3HY")[0] == family + "Y"
    assert rows("\U0001f44d\U0001f3fd\x1b[1;3HY")[0] == "\U0001f44d\U0001f3fdY"
    assert rows("x\u200dy\x1b[1;3HY")[0] == "x\u200dyY"


def test_hangul_jamo_join_into_one_wide_cell():
    assert rows("\u1100\u1161\x1b[1;3HY")[0] == "\u1100\u1161Y"


def test_marker_characters_are_one_cell():
    assert rows("\u276f a\x1b[1;3HY")[0] == "\u276f Y"
    assert rows("\u23fa \u25cf \u00b7")[0] == "\u23fa \u25cf \u00b7"


def test_soft_hyphen_takes_a_cell():
    assert rows("\u00ad\x1b[1;2HY")[0] == "\u00adY"


def test_utf8_split_across_feeds():
    s = Screen(10, 2)
    data = "中\u276f".encode()
    for i in range(len(data)):
        s.feed(data[i : i + 1])
    assert s.lines()[0] == "中\u276f"


def test_invalid_utf8_shows_a_replacement_character():
    assert rows(b"a\xffb")[0] == "a\ufffdb"


# Control characters.


def test_backspace_stops_at_the_left_edge():
    assert rows("ab\b\bX\b\b\bY")[0] == "Yb"


def test_tab_stops_every_eight_columns():
    assert rows("a\tb\tc")[0] == "a       b       c"
    assert rows("\t\t\t\tX")[0] == " " * 19 + "X"


def test_tab_set_clear_forward_and_back():
    assert rows("\x1b[3g\x1b[1;4H\x1bH\x1b[1;1H\tX")[0] == "   X"
    assert rows("\x1b[1;1H\x1b[2IX")[0] == " " * 16 + "X"
    assert rows("\x1b[1;18H\x1b[ZX")[0] == " " * 16 + "X"
    assert rows("\x1b[1;9H\x1b[g\x1b[1;1H\tX")[0] == " " * 16 + "X"


def test_bell_and_other_controls_are_ignored():
    assert rows("a\x07\x00\x0e\x0f\x7fb")[0] == "ab"


# Scrolling.


def test_line_feed_scrolls_at_the_bottom():
    assert rows("1\r\n2\r\n3\r\n4", rows=3) == ["2", "3", "4"]


def test_scroll_region_scrolls_only_inside():
    s = screen("1\r\n2\r\n3\r\n4\r\n5\x1b[2;4r\x1b[4;1H\nX")
    assert s.lines() == ["1", "3", "4", "X", "5"]


def test_reverse_index_scrolls_down_at_the_top_of_the_region():
    s = screen("1\r\n2\r\n3\r\n4\r\n5\x1b[2;4r\x1b[2;1H\x1bMX")
    assert s.lines() == ["1", "X", "2", "3", "5"]
    assert rows("\x1b[3;1H\x1bMX")[1] == "X"


def test_reverse_index_at_the_top_of_the_screen_scrolls_down():
    assert rows("a\x1bMb")[:2] == [" b", "a"]


def test_index_and_next_line():
    assert rows("a\x1bDb\x1bEc")[:3] == ["a", " b", "c"]


def test_scroll_up_and_down():
    assert rows("1\r\n2\r\n3\x1b[S", rows=3) == ["2", "3", ""]
    assert rows("1\r\n2\r\n3\x1b[2T", rows=3) == ["", "", "1"]


def test_scroll_down_with_mouse_tracking_parameters_is_ignored():
    assert rows("1\x1b[1;2;3;4;5T", rows=3) == ["1", "", ""]


def test_invalid_region_is_ignored():
    s = screen("1\r\n2\r\n3\x1b[3;2r\r\n4\r\n5\r\n6")
    assert s.lines() == ["2", "3", "4", "5", "6"]


def test_region_resets_on_empty_parameters_and_homes_the_cursor():
    s = screen("abc\x1b[2;3r")
    assert s.cursor == (0, 0)
    s.feed(b"\x1b[r\x1b[5;1H\n")
    assert s.lines()[0] == ""


# Cursor movement.


@pytest.mark.parametrize(
    ("seq", "cursor"),
    [
        ("\x1b[3;4H", (2, 3)),
        ("\x1b[H", (0, 0)),
        ("\x1b[;5H", (0, 4)),
        ("\x1b[3H", (2, 0)),
        ("\x1b[99;99H", (4, 19)),
        ("\x1b[0;0f", (0, 0)),
        ("\x1b[3;4H\x1b[A", (1, 3)),
        ("\x1b[3;4H\x1b[9A", (0, 3)),
        ("\x1b[3;4H\x1b[B", (3, 3)),
        ("\x1b[3;4H\x1b[9B", (4, 3)),
        ("\x1b[3;4H\x1b[C", (2, 4)),
        ("\x1b[3;4H\x1b[99C", (2, 19)),
        ("\x1b[3;4H\x1b[D", (2, 2)),
        ("\x1b[3;4H\x1b[99D", (2, 0)),
        ("\x1b[3;4H\x1b[E", (3, 0)),
        ("\x1b[3;4H\x1b[2F", (0, 0)),
        ("\x1b[3;4H\x1b[7G", (2, 6)),
        ("\x1b[3;4H\x1b[G", (2, 0)),
        ("\x1b[3;4H\x1b[5d", (4, 3)),
        ("\x1b[3;4H\x1b[d", (0, 3)),
        ("\x1b[3;4H\x1b[0A", (1, 3)),
    ],
)
def test_cursor_movement(seq, cursor):
    assert screen(seq).cursor == cursor


def test_cursor_up_and_down_stop_at_the_region_margins():
    assert screen("\x1b[2;4r\x1b[3;1H\x1b[9A").cursor == (1, 0)
    assert screen("\x1b[2;4r\x1b[3;1H\x1b[9B").cursor == (3, 0)
    assert screen("\x1b[2;4r\x1b[5;1H\x1b[9A").cursor == (1, 0)


def test_origin_mode_counts_rows_from_the_region():
    s = screen("\x1b[2;4r\x1b[?6h\x1b[1;1HX\x1b[9;1HY")
    assert s.lines() == ["", "X", "", "Y", ""]
    s.feed(b"\x1b[?6l\x1b[1;1HZ")
    assert s.lines()[0] == "Z"


def test_save_and_restore_cursor():
    assert rows("\x1b[2;3H\x1b7\x1b[5;5H\x1b8X")[1] == "  X"
    assert rows("\x1b[2;3H\x1b[s\x1b[5;5H\x1b[uX")[1] == "  X"


def test_restore_without_save_goes_home():
    assert screen("\x1b[3;3H\x1b8").cursor == (0, 0)


# Erasing and editing.


def test_erase_display():
    base = "11111\r\n22222\r\n33333\x1b[2;3H"
    assert rows(base + "\x1b[J", cols=5) == ["11111", "22", "", "", ""]
    assert rows(base + "\x1b[0J", cols=5) == ["11111", "22", "", "", ""]
    assert rows(base + "\x1b[1J", cols=5) == ["", "   22", "33333", "", ""]
    assert rows(base + "\x1b[2J", cols=5) == [""] * 5
    assert rows(base + "\x1b[3J", cols=5) == ["11111", "22222", "33333", "", ""]


def test_erase_display_keeps_the_cursor():
    assert screen("abc\x1b[2J").cursor == (0, 3)


def test_erase_line():
    base = "abcde\x1b[1;3H"
    assert rows(base + "\x1b[K")[0] == "ab"
    assert rows(base + "\x1b[1K")[0] == "   de"
    assert rows(base + "\x1b[2K")[0] == ""


def test_insert_and_delete_characters():
    assert rows("abcde\x1b[1;2H\x1b[2@", cols=5)[0] == "a  bc"
    assert rows("abcde\x1b[1;2H\x1b[2P", cols=5)[0] == "ade"
    assert rows("abcde\x1b[1;2H\x1b[99P", cols=5)[0] == "a"
    assert rows("abcde\x1b[1;2H\x1b[2X", cols=5)[0] == "a  de"


def test_insert_and_delete_lines_inside_the_region():
    base = "1\r\n2\r\n3\r\n4\r\n5"
    assert rows(base + "\x1b[2;1H\x1b[L") == ["1", "", "2", "3", "4"]
    assert rows(base + "\x1b[2;1H\x1b[2M") == ["1", "4", "5", "", ""]
    assert rows(base + "\x1b[2;4r\x1b[3;1H\x1b[L") == ["1", "2", "", "3", "5"]
    assert rows(base + "\x1b[2;4r\x1b[5;1H\x1b[L") == ["1", "2", "3", "4", "5"]


def test_insert_line_keeps_the_column_like_tmux():
    assert screen("abc\x1b[1;3H\x1b[L").cursor == (0, 2)


def test_insert_mode_shifts_text_right():
    assert rows("abc\x1b[1;1H\x1b[4hXY\x1b[4lZ", cols=5)[0] == "XYZbc"


def test_repeat_last_character():
    assert rows("a\x1b[3b")[0] == "aaaa"


def test_screen_alignment_fills_with_e():
    assert rows("\x1b#8", cols=3, rows=2) == ["EEE", "EEE"]


# Sequences that change no text.


def test_sgr_is_dropped():
    assert rows("\x1b[1;31;48;2;1;2;3mred\x1b[38:2::1:2:3mX\x1b[0m")[0] == "redX"


def test_osc_ends_with_bel_or_st():
    assert rows("a\x1b]0;title\x07b\x1b]8;;http://x\x1b\\c")[0] == "abc"


def test_dcs_apc_pm_sos_are_skipped():
    data = "a\x1bPq#0;1\x1b\\b\x1b_Gi=1;AAAA\x1b\\c\x1b^pm\x1b\\d\x1bXsos\x1b\\e"
    assert rows(data)[0] == "abcde"


def test_string_ends_at_cancel():
    assert rows("a\x1b]0;title\x18b")[0] == "ab"


def test_charset_selection_is_skipped():
    assert rows("a\x1b(Bb\x1b)0c\x1b*Ad")[0] == "abcd"


def test_queries_and_unknown_sequences_change_nothing():
    queries = "\x1b[c\x1b[>0q\x1b[?u\x1b[>1u\x1b[<u\x1b[6n\x1b[16t\x1b[?2026$p"
    data = "a" + queries + "b\x1b[ q\x1b[!pc\x1b[999zd"
    s = screen(data)
    assert s.lines()[0] == "abcd"
    assert s.cursor == (0, 4)


def test_controls_inside_a_csi_run_in_place():
    assert rows("ab\x1b[\r1Cc")[0] == "ac"


def test_escape_inside_a_csi_starts_a_new_sequence():
    assert rows("a\x1b[12\x1b[3Gb")[0] == "a b"


def test_huge_parameters_are_clamped():
    s = screen("\x1b[99999999999999999999;99999999999999999999H\x1b[999999999@\x1b[99999999L")
    assert s.cursor == (4, 19)


# Modes.


def test_application_cursor_keys_and_bracketed_paste():
    s = screen("\x1b[?1h\x1b[?2004h")
    assert s.application_cursor_keys and s.bracketed_paste
    s.feed(b"\x1b[?1l\x1b[?2004l")
    assert not s.application_cursor_keys and not s.bracketed_paste


def test_several_private_modes_in_one_sequence():
    s = screen("\x1b[?1;2004;25h")
    assert s.application_cursor_keys and s.bracketed_paste


def test_input_modes_follow_set_and_reset():
    s = screen("\x1b[?1000;1002;1006h\x1b[?2004h\x1b[?1004h\x1b[?25l\x1b[?2026h")
    assert s.input_modes == [1000, 1002, 1004, 1006, 2004]
    s.feed(b"\x1b[?1002l\x1b[?1004l")
    assert s.input_modes == [1000, 1006, 2004]
    s.feed(b"\x1bc")
    assert s.input_modes == []


def test_cursor_visibility_and_synchronized_output_change_no_text():
    assert rows("a\x1b[?25l\x1b[?2026hb\x1b[?2026l\x1b[?25h")[0] == "ab"


def test_alternate_screen_1049_saves_the_cursor_and_clears():
    s = screen("main\x1b[2;3H\x1b[?1049h")
    assert s.alternate
    assert s.lines() == [""] * 5
    s.feed(b"\x1b[5;5Halt\x1b[?1049l")
    assert not s.alternate
    assert s.lines()[0] == "main"
    assert s.cursor == (1, 2)


def test_alternate_screen_is_cleared_each_time():
    s = screen("\x1b[?1049hold\x1b[?1049l\x1b[?1049h")
    assert s.lines() == [""] * 5


@pytest.mark.parametrize("mode", [47, 1047])
def test_older_alternate_modes_keep_the_cursor(mode):
    s = screen(f"main\x1b[?{mode}hx\x1b[?{mode}l")
    assert s.lines()[0] == "main"
    assert s.cursor == (0, 5)


def test_full_reset():
    s = screen("abc\x1b[?1h\x1b[?2004h\x1b[?1049h\x1b[2;3rxyz\x1bc")
    assert s.lines() == [""] * 5
    assert s.cursor == (0, 0)
    assert not (s.alternate or s.bracketed_paste or s.application_cursor_keys)


# Resize.


def test_resize_keeps_content_at_the_top_left():
    s = screen("abcdef\r\n12345\r\nxyz\x1b[5;20H", cols=20)
    s.resize(4, 2)
    assert s.lines() == ["abcd", "1234"]
    assert s.cursor == (1, 3)
    s.resize(10, 4)
    assert s.lines() == ["abcd", "1234", "", ""]
    s.feed(b"\x1b[4;10HZ")
    assert s.lines()[3] == "         Z"


def test_resize_through_a_wide_character_blanks_it():
    s = screen("ab中")
    s.resize(3, 5)
    assert s.lines()[0] == "ab"


def test_resize_applies_to_both_buffers():
    s = screen("main\x1b[?1049halt")
    s.resize(2, 2)
    s.feed(b"\x1b[?1049l")
    assert s.lines() == ["ma", ""]


def test_resize_resets_the_region():
    s = screen("\x1b[2;3r")
    s.resize(20, 3)
    s.feed(b"1\r\n2\r\n3\r\n4")
    assert s.lines() == ["2", "3", "4"]


# Replay.


def _replayed(s: Screen) -> Screen:
    fresh = Screen(s.cols, s.rows)
    fresh.feed(s.replay())
    return fresh


def test_replay_repaints_text_and_cursor():
    s = screen("\x1b[31mhello\x1b[0m\r\n  中文 e\u0301\x1b[4;7H")
    out = s.replay()
    assert out.startswith(b"\x1b[H\x1b[2J")
    assert b"\x1b[31m" not in out
    copy = _replayed(s)
    assert copy.lines() == s.lines()
    assert copy.cursor == s.cursor == (3, 6)


def test_replay_sets_input_modes_before_the_repaint():
    s = screen("\x1b[?1h\x1b[?2004h\x1b[?1003h\x1b[?1006hhi")
    out = s.replay()
    assert out.startswith(b"\x1b[?1h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H\x1b[2J")
    copy = _replayed(s)
    assert copy.input_modes == s.input_modes
    assert copy.lines() == s.lines()


def test_replay_keeps_a_pending_wrap():
    s = screen("abcde", cols=5, rows=2)
    copy = _replayed(s)
    copy.feed(b"f")
    s.feed(b"f")
    assert copy.lines() == s.lines() == ["abcde", "f"]


def test_replay_of_a_full_screen_does_not_scroll():
    s = screen("abcdefghij", cols=5, rows=2)
    assert _replayed(s).lines() == ["abcde", "fghij"]


# Robustness.


def _chunked_equals_whole(data: bytes, cols: int = 30, rows: int = 8) -> None:
    whole = Screen(cols, rows)
    whole.feed(data)
    split = Screen(cols, rows)
    for i in range(len(data)):
        split.feed(data[i : i + 1])
    assert split.lines() == whole.lines()
    assert split.cursor == whole.cursor
    assert split.alternate == whole.alternate


_ALPHABET = [
    b"\x1b", b"[", b"]", b"?", b";", b"1", b"0", b"9", b"h", b"l", b"H", b"J", b"K", b"m",
    b"r", b"@", b"P", b"L", b"M", b"\x07", b"\\", b"\r", b"\n", b"\b", b"\t", b"a", b"Z",
    b"P", b"_", b"(", b"#", b"8", b"7", b"\x18", "中".encode(), "\u276f".encode(),
    "\u0301".encode(), "\ufe0f".encode(), "\u200d".encode(), "\U0001f44d".encode(),
    b"\xe4", b"\xbd", b"\xff", b"\x9b", b"1049", b"2004", b"6", b"S", b"T", b"X", b"b",
]  # fmt: skip


@pytest.mark.parametrize("seed", range(40))
def test_random_bytes_never_raise_and_chunking_does_not_matter(seed):
    rng = random.Random(seed)
    data = bytes(rng.getrandbits(8) for _ in range(3000))
    _chunked_equals_whole(data)


@pytest.mark.parametrize("seed", range(60))
def test_random_escape_heavy_streams(seed):
    rng = random.Random(1000 + seed)
    data = b"".join(rng.choice(_ALPHABET) for _ in range(1500))
    _chunked_equals_whole(data, cols=rng.randint(1, 12), rows=rng.randint(1, 6))
    s = Screen(rng.randint(1, 12), rng.randint(1, 6))
    s.feed(data)
    s.resize(rng.randint(1, 12), rng.randint(1, 6))
    s.feed(data[::-1])
    assert len(s.lines()) == s.rows
    assert all(len(line) <= s.cols * 40 for line in s.lines())
    _replayed(s)


def test_one_column_screen_handles_wide_characters():
    s = screen("中a\u0301\ufe0fb", cols=1, rows=3)
    assert s.lines() == ["a\u0301", "b", ""]


# Recorded streams from real programs, with tmux capture-pane output as the expected text.


def _fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.bin"))


@pytest.mark.parametrize("name", _fixture_names())
def test_recorded_stream_matches_tmux(name):
    data = (FIXTURES / f"{name}.bin").read_bytes()
    want = (FIXTURES / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n")
    s = Screen(80, 24)
    s.feed(data)
    assert s.text() == want
    _chunked_equals_whole(data, cols=80, rows=24)


def test_large_stream_is_fast_enough():
    import time

    data = (b"\x1b[32m" + X80 + b"\x1b[0m\r\n" + "中文\u276f ".encode() * 10 + b"\r\n") * 2000
    s = Screen(80, 24)
    start = time.perf_counter()
    s.feed(data)
    assert time.perf_counter() - start < 5


@pytest.mark.parametrize("seed", range(20))
def test_ascii_fast_path_equals_printing_each_character(seed):
    rng = random.Random(seed)
    cols, rows_ = rng.randint(1, 9), rng.randint(1, 4)
    setup = b"".join(rng.choice(_ALPHABET) for _ in range(200))
    fast, slow = Screen(cols, rows_), Screen(cols, rows_)
    fast.feed(setup + b"\x1b[0m")
    slow.feed(setup + b"\x1b[0m")
    if fast._insert or not fast._autowrap or fast._state:
        return
    run = "".join(rng.choice("ab cd") for _ in range(rng.randint(1, 30)))
    fast._print_ascii(run)
    for ch in run:
        slow._print(ch)
    assert fast.lines() == slow.lines()
    assert fast._x == slow._x and fast._y == slow._y
