"""Tests for global hotkeys: Ctrl+Shift dictate, Alt+Space talk, Ctrl+Alt+Q quit, and chord safety."""
import pytest
from pynput.keyboard import Key, KeyCode

from wren.cli import (
    DEFAULT_HOTKEY,
    DEFAULT_QUIT_HOTKEY,
    DEFAULT_TALK_HOTKEY,
    _canon_key,
    _parse_hotkey,
)


def test_default_hotkeys_preserve_openwhispr_ctrl_alt():
    """Ctrl+Shift dictates and Alt+Space talks so OpenWhispr on Ctrl+Alt never collides."""
    assert DEFAULT_HOTKEY == "<ctrl>+<shift>"
    assert DEFAULT_TALK_HOTKEY == "<alt>+<space>"
    assert DEFAULT_QUIT_HOTKEY == "<ctrl>+<alt>+q"
    assert len(_parse_hotkey(DEFAULT_HOTKEY)) == 2
    assert len(_parse_hotkey(DEFAULT_TALK_HOTKEY)) == 2


def test_canon_key_normalizes_left_right_modifiers_and_keycode_chars():
    assert _canon_key(Key.ctrl_l) == Key.ctrl
    assert _canon_key(Key.ctrl_r) == Key.ctrl
    assert _canon_key(Key.shift_l) == Key.shift
    assert _canon_key(Key.shift_r) == Key.shift
    assert _canon_key(Key.alt_l) == Key.alt
    assert _canon_key(Key.alt_r) == Key.alt
    assert _canon_key(KeyCode.from_char("q")) == "q"
    assert _canon_key(KeyCode.from_char("Q")) == "q"


def test_parse_empty_spec_yields_empty_set():
    assert _parse_hotkey("") == set()


def test_parse_rejects_unknown_named_key():
    with pytest.raises(ValueError):
        _parse_hotkey("<ctrl>+<shift>+f9")
