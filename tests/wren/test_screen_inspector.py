"""Tests for Wren Screen & Interface Inspector."""
from unittest.mock import patch

from wren.screen.inspector import ScreenContext, ScreenInspector


def test_screen_inspector_linux_x11():
    with patch("sys.platform", "linux"):
        inspector = ScreenInspector()
        with patch("subprocess.check_output") as mock_cmd:
            # Mock xdotool getactivewindow
            def fake_check_output(cmd, **kwargs):
                if cmd[0] == "xdotool":
                    return b"1234567\n"
                elif cmd[0] == "xprop":
                    return (
                        b'WM_CLASS(STRING) = "google-chrome", "Google-chrome"\n'
                        b'_NET_WM_NAME(UTF8_STRING) = "GitHub - DGFcorporations/Ren - Google Chrome"\n'
                    )
                return b""

            mock_cmd.side_effect = fake_check_output

            ctx = inspector.get_active_screen_context()
            assert isinstance(ctx, ScreenContext)
            assert "Google-chrome" in ctx.app_name or "google-chrome" in ctx.app_name
            assert "GitHub - DGFcorporations/Ren" in ctx.window_title
            assert ctx.is_browser is True
            assert ctx.is_terminal is False


def test_screen_inspector_fallback_on_error():
    inspector = ScreenInspector()
    with patch("subprocess.check_output", side_effect=Exception("command failed")):
        ctx = inspector.get_active_screen_context()
        assert ctx.app_name == "Unknown"
        assert ctx.window_title == "Unknown Window"
