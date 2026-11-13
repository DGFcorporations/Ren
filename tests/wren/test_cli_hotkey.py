"""Tests for the global push-to-talk hotkey parser in wren.cli."""
import pytest

from wren.cli import DEFAULT_HOTKEY, DEFAULT_QUIT_HOTKEY, _parse_hotkey


def test_parse_default_hotkey():
    keys = _parse_hotkey(DEFAULT_HOTKEY)
    assert len(keys) == 3


def test_parse_quit_hotkey():
    keys = _parse_hotkey(DEFAULT_QUIT_HOTKEY)
    assert len(keys) == 3
    assert "q" in keys


def test_parse_empty_spec_yields_empty_set():
    # Callers treat an empty combo as "disabled" rather than a valid binding.
    assert _parse_hotkey("") == set()


def test_parse_rejects_unknown_named_key():
    with pytest.raises(ValueError):
        _parse_hotkey("<ctrl>+<shift>+f9")
