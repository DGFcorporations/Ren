"""Wren Screen Inspector: Context and interface reading across Linux and Windows."""
import base64
import re
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class ScreenContext:
    app_name: str
    window_title: str
    window_id: str
    is_browser: bool
    is_editor: bool
    is_terminal: bool
    timestamp: float
    screenshot_b64: str | None = None


BROWSER_NAMES = {
    "google-chrome",
    "chrome",
    "chromium",
    "firefox",
    "brave-browser",
    "brave",
    "msedge",
    "edge",
    "safari",
}
EDITOR_NAMES = {
    "code",
    "cursor",
    "vscodium",
    "sublime_text",
    "pycharm",
    "clion",
    "webstorm",
    "idea",
    "vim",
    "nvim",
}
TERMINAL_NAMES = {
    "gnome-terminal",
    "alacritty",
    "kitty",
    "xterm",
    "tilix",
    "konsole",
    "terminator",
    "wezterm",
    "cmd.exe",
    "powershell.exe",
    "windowsterminal.exe",
}


class ScreenInspector:
    """Extracts active window, focused application, and screen context."""

    def __init__(self):
        self.is_windows = sys.platform == "win32"

    def get_active_screen_context(self, capture_screenshot: bool = False) -> ScreenContext:
        """Inspect the current focused desktop interface."""
        now = time.time()
        try:
            if self.is_windows:
                return self._get_windows_context(now, capture_screenshot)
            else:
                return self._get_linux_context(now, capture_screenshot)
        except Exception:
            return ScreenContext(
                app_name="Unknown",
                window_title="Unknown Window",
                window_id="0",
                is_browser=False,
                is_editor=False,
                is_terminal=False,
                timestamp=now,
            )

    def _get_linux_context(self, now: float, capture_screenshot: bool) -> ScreenContext:
        # 1. Get active window ID via xdotool
        out = subprocess.check_output(
            ["xdotool", "getactivewindow"],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        ).decode().strip()
        win_id = out.splitlines()[0]

        # 2. Get WM_CLASS and _NET_WM_NAME via xprop
        xprop_out = subprocess.check_output(
            ["xprop", "-id", win_id, "WM_CLASS", "_NET_WM_NAME"],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        ).decode()

        wm_class = "Unknown"
        title = "Untitled"

        m_class = re.search(r'WM_CLASS\(STRING\) = "(.*?)", "(.*?)"', xprop_out)
        if m_class:
            wm_class = m_class.group(2)
        else:
            m_simple = re.search(r'WM_CLASS\(.*\) = "(.*?)"', xprop_out)
            if m_simple:
                wm_class = m_simple.group(1)

        m_name = re.search(r'_NET_WM_NAME\(.*?\) = "(.*?)"', xprop_out)
        if m_name:
            title = m_name.group(1)

        app_lower = wm_class.lower()
        is_browser = any(b in app_lower for b in BROWSER_NAMES)
        is_editor = any(e in app_lower for e in EDITOR_NAMES)
        is_terminal = any(t in app_lower for t in TERMINAL_NAMES)

        screenshot_b64 = None
        if capture_screenshot:
            screenshot_b64 = self._capture_linux_window(win_id)

        return ScreenContext(
            app_name=wm_class,
            window_title=title,
            window_id=win_id,
            is_browser=is_browser,
            is_editor=is_editor,
            is_terminal=is_terminal,
            timestamp=now,
            screenshot_b64=screenshot_b64,
        )

    def _get_windows_context(self, now: float, capture_screenshot: bool) -> ScreenContext:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ScreenContext(
                app_name="Desktop",
                window_title="Desktop",
                window_id="0",
                is_browser=False,
                is_editor=False,
                is_terminal=False,
                timestamp=now,
            )

        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        title = title_buf.value or "Untitled"

        class_buf = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, class_buf, 512)
        cls_name = class_buf.value or "Unknown"

        app_lower = cls_name.lower()
        is_browser = any(b in app_lower or b in title.lower() for b in BROWSER_NAMES)
        is_editor = any(e in app_lower for e in EDITOR_NAMES)
        is_terminal = any(t in app_lower for t in TERMINAL_NAMES)

        return ScreenContext(
            app_name=cls_name,
            window_title=title,
            window_id=str(hwnd),
            is_browser=is_browser,
            is_editor=is_editor,
            is_terminal=is_terminal,
            timestamp=now,
        )

    def _capture_linux_window(self, win_id: str) -> str | None:
        try:
            # Try scrot or import
            img_data = subprocess.check_output(
                ["import", "-window", win_id, "png:-"],
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
            return base64.b64encode(img_data).decode("utf-8")
        except Exception:
            return None
