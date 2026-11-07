"""Tests for Wren X11 clipboard adapter."""
import hashlib
from unittest.mock import MagicMock, patch

import pytest

from wren.clipboard.x11 import ClipboardError, X11Clipboard


def test_clipboard_snapshot_and_hash():
    clip = X11Clipboard()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="Initial clipboard text")
        snap = clip.snapshot()
        assert snap.content == "Initial clipboard text"
        assert snap.content_hash == hashlib.sha256(b"Initial clipboard text").hexdigest()


def test_clipboard_write_and_readback_verify():
    clip = X11Clipboard()
    test_text = "Elevated text for testing: Hello World! 🚀"
    
    with patch("subprocess.run") as mock_run:
        # First write (input text)
        mock_run.side_effect = [
            MagicMock(returncode=0),                                # xclip -selection clipboard -i
            MagicMock(returncode=0, stdout=test_text),             # xclip -selection clipboard -o
        ]
        written_hash = clip.write(test_text)
        expected_hash = hashlib.sha256(test_text.encode("utf-8")).hexdigest()
        assert written_hash == expected_hash


def test_clipboard_readback_mismatch_raises_error():
    clip = X11Clipboard()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),                                # write succeeds
            MagicMock(returncode=0, stdout="Corrupted text"),       # readback differs
        ]
        with pytest.raises(ClipboardError, match="Readback verification failed"):
            clip.write("Expected text")


def test_clipboard_safe_restore_prevents_clobbering_external_copy():
    clip = X11Clipboard()
    
    # Snapshot original
    original_text = "Josh's original notes"
    wren_text = "Wren's generated draft"
    
    with patch("subprocess.run") as mock_run:
        # 1. Snapshot
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=original_text),
        ]
        snap = clip.snapshot()
    
    # Scenario A: Clipboard still holds Wren's text -> Restore is safe
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=wren_text),  # current clipboard check
            MagicMock(returncode=0),                    # restore write
        ]
        restored = clip.safe_restore(snap, wren_written_text=wren_text)
        assert restored is True

    # Scenario B: Josh copied something new in the meantime -> Do NOT restore!
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="Josh copied something new"),  # current differs from Wren text
        ]
        restored = clip.safe_restore(snap, wren_written_text=wren_text)
        assert restored is False
