"""Tests for Wren Windows Win32 Target and Clipboard Adapters."""
import ctypes
from unittest.mock import MagicMock, patch

import pytest

from wren.clipboard.windows import WindowsClipboard
from wren.targets.win32 import TargetMismatchError, Win32TargetManager, Win32TargetWindow


def test_win32_get_active_target_mock():
    mock_windll = MagicMock()
    mock_user32 = MagicMock()
    mock_windll.user32 = mock_user32

    mock_user32.GetForegroundWindow.return_value = 99999

    # Simulate GetWindowTextW and GetClassNameW
    def fake_get_text(hwnd, buf, max_len):
        buf.value = "Document - Word"
        return len(buf.value)

    def fake_get_class(hwnd, buf, max_len):
        buf.value = "OpusApp"
        return len(buf.value)

    mock_user32.GetWindowTextW.side_effect = fake_get_text
    mock_user32.GetClassNameW.side_effect = fake_get_class

    with patch.object(ctypes, "windll", mock_windll, create=True):
        mgr = Win32TargetManager()
        target = mgr.get_active_target()

        assert target.window_id == "99999"
        assert target.title == "Document - Word"
        assert target.wm_class == "OpusApp"
        assert target.is_terminal is False


def test_win32_paste_target_mismatch_aborts():
    mock_windll = MagicMock()
    mock_user32 = MagicMock()
    mock_windll.user32 = mock_user32

    # Foreground window is different
    mock_user32.GetForegroundWindow.return_value = 11111

    target = Win32TargetWindow(
        window_id="99999",
        wm_class="OpusApp",
        title="Document - Word",
        is_terminal=False,
    )

    with patch.object(ctypes, "windll", mock_windll, create=True):
        mgr = Win32TargetManager()
        with pytest.raises(TargetMismatchError):
            mgr.paste(target)


def test_windows_clipboard_snapshot_and_hash():
    with patch("pyperclip.paste", return_value="Test windows text"):
        clip = WindowsClipboard()
        snap = clip.snapshot()
        assert snap.content == "Test windows text"
        assert len(snap.content_hash) == 64
