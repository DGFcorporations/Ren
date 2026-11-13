"""Tests for Wren X11 target window detection and paste execution."""
import os
from unittest.mock import MagicMock, patch

import pytest

from wren.targets.x11 import TargetMismatchError, TargetWindow, X11TargetManager


def test_get_active_target():
    manager = X11TargetManager()
    with patch("subprocess.run") as mock_run:
        # xdotool getactivewindow -> "12345"
        # xprop -id 12345 WM_CLASS -> WM_CLASS(STRING) = "gedit", "Gedit"
        # xdotool getwindowname 12345 -> "Untitled Document 1 - gedit"
        # xdotool getwindowpid 12345 -> "54321"
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="12345\n"),
            MagicMock(returncode=0, stdout='WM_CLASS(STRING) = "gedit", "Gedit"\n'),
            MagicMock(returncode=0, stdout="Untitled Document 1 - gedit\n"),
            MagicMock(returncode=0, stdout="54321\n"),
        ]
        target = manager.get_active_target()
        assert target.window_id == "12345"
        assert target.wm_class == "gedit"
        assert target.title == "Untitled Document 1 - gedit"
        assert target.pid == 54321
        assert target.is_terminal is False
        assert target.target_hash is not None


def test_detect_terminal_target():
    manager = X11TargetManager()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="9999\n"),
            MagicMock(returncode=0, stdout='WM_CLASS(STRING) = "gnome-terminal", "Gnome-terminal"\n'),
            MagicMock(returncode=0, stdout="bash\n"),
            MagicMock(returncode=0, stdout="7777\n"),
        ]
        target = manager.get_active_target()
        assert target.is_terminal is True


def test_verify_active_target_mismatch():
    manager = X11TargetManager()
    t1 = TargetWindow(
        window_id="111",
        wm_class="text-editor",
        title="Notes",
        pid=100,
        target_hash="hash-editor",
        is_terminal=False,
    )
    
    with patch.object(manager, "get_active_target") as mock_get:
        mock_get.return_value = TargetWindow(
            window_id="222",
            wm_class="web-browser",
            title="Search",
            pid=200,
            target_hash="hash-browser",
            is_terminal=False,
        )
        assert manager.verify_target(t1) is False


def test_paste_executes_shortcut_without_enter():
    manager = X11TargetManager()
    t1 = TargetWindow(
        window_id="12345",
        wm_class="gedit",
        title="Document",
        pid=54321,
        target_hash="hash-12345",
        is_terminal=False,
    )
    
    with patch.object(manager, "get_active_target", return_value=t1), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        
        receipt = manager.paste_into_target(t1)
        assert receipt["status"] == "pasted"
        assert receipt["window_id"] == "12345"
        
        # Verify xdotool key was Ctrl+v, NOT Return / Enter
        mock_run.assert_called_with(
            ["xdotool", "key", "--window", "12345", "ctrl+v"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )


def test_paste_uses_ctrl_shift_v_for_terminal():
    manager = X11TargetManager()
    term = TargetWindow(
        window_id="55555",
        wm_class="gnome-terminal",
        title="Terminal",
        pid=99999,
        target_hash="hash-term",
        is_terminal=True,
    )
    
    with patch.object(manager, "get_active_target", return_value=term), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        
        receipt = manager.paste_into_target(term)
        assert receipt["status"] == "pasted"
        
        mock_run.assert_called_with(
            ["xdotool", "key", "--window", "55555", "ctrl+shift+v"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )


def test_paste_aborts_if_window_switched():
    manager = X11TargetManager()
    original_target = TargetWindow(
        window_id="11111",
        wm_class="gedit",
        title="Doc",
        pid=1000,
        target_hash="hash-orig",
        is_terminal=False,
    )
    
    switched_target = TargetWindow(
        window_id="22222",
        wm_class="slack",
        title="Chat",
        pid=2000,
        target_hash="hash-slack",
        is_terminal=False,
    )
    
    with patch.object(manager, "get_active_target", return_value=switched_target):
        with pytest.raises(TargetMismatchError, match="Active window changed"):
            manager.paste_into_target(original_target)


def _mock_window_queries(mock_run, window_id="12345", wm_class="gedit",
                         title="Notes - gedit", pid=54321):
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout=f"{window_id}\n"),
        MagicMock(returncode=0, stdout=f'WM_CLASS(STRING) = "{wm_class}", "{wm_class.title()}"\n'),
        MagicMock(returncode=0, stdout=f"{title}\n"),
        MagicMock(returncode=0, stdout=f"{pid}\n"),
    ]


def test_self_window_detected_by_pid():
    manager = X11TargetManager()
    with patch("subprocess.run") as mock_run:
        _mock_window_queries(mock_run, pid=os.getpid())
        target = manager.get_active_target()
        assert target.is_self is True


def test_self_window_detected_by_title():
    manager = X11TargetManager()
    with patch("subprocess.run") as mock_run:
        _mock_window_queries(mock_run, title="Wren Assistant", pid=99999)
        target = manager.get_active_target()
        assert target.is_self is True


def test_normal_window_is_not_self():
    manager = X11TargetManager()
    with patch("subprocess.run") as mock_run:
        _mock_window_queries(mock_run)
        target = manager.get_active_target()
        assert target.is_self is False


def test_get_paste_target_returns_remembered_external_when_self_focused():
    manager = X11TargetManager()
    editor = TargetWindow(
        window_id="111", wm_class="gedit", title="Doc", pid=100,
        target_hash="h1", is_terminal=False, is_self=False,
    )
    self_win = TargetWindow(
        window_id="222", wm_class="WebKit", title="Wren Assistant", pid=os.getpid(),
        target_hash="h2", is_terminal=False, is_self=True,
    )
    manager._last_external = editor
    with patch.object(manager, "get_active_target", return_value=self_win):
        assert manager.get_paste_target() is editor


def test_get_paste_target_raises_when_self_focused_and_no_memory():
    manager = X11TargetManager()
    self_win = TargetWindow(
        window_id="222", wm_class="WebKit", title="Wren Assistant", pid=os.getpid(),
        target_hash="h2", is_terminal=False, is_self=True,
    )
    with patch.object(manager, "get_active_target", return_value=self_win):
        with pytest.raises(RuntimeError, match="No external target"):
            manager.get_paste_target()


def test_get_paste_target_caches_external_window():
    manager = X11TargetManager()
    editor = TargetWindow(
        window_id="111", wm_class="gedit", title="Doc", pid=100,
        target_hash="h1", is_terminal=False, is_self=False,
    )
    with patch.object(manager, "get_active_target", return_value=editor):
        assert manager.get_paste_target() is editor
    assert manager._last_external is editor


def test_paste_into_target_self_focus_activates_window_first():
    manager = X11TargetManager()
    editor = TargetWindow(
        window_id="12345", wm_class="gedit", title="Doc", pid=54321,
        target_hash="hash-12345", is_terminal=False, is_self=False,
    )
    self_win = TargetWindow(
        window_id="999", wm_class="WebKit", title="Wren Assistant", pid=os.getpid(),
        target_hash="hash-self", is_terminal=False, is_self=True,
    )
    with patch.object(manager, "get_active_target", return_value=self_win), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        receipt = manager.paste_into_target(editor)
        assert receipt["status"] == "pasted"

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["xdotool", "windowactivate", "--sync", "12345"] in calls
        # Real key events go to the now-focused window — no --window flag.
        assert ["xdotool", "key", "ctrl+v"] in calls
        assert not any("--window" in c for c in calls)
