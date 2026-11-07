"""Wren Windows Win32 Target Manager: Focus tracking and verified paste on Windows."""
import ctypes
import time
from dataclasses import dataclass


class TargetMismatchError(RuntimeError):
    """Raised when active foreground window does not match the approved target."""
    pass


@dataclass
class Win32TargetWindow:
    window_id: str
    wm_class: str
    title: str
    is_terminal: bool = False


WIN_TERMINAL_CLASSES = {
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
    "mintty",
    "Alacritty",
    "KiTTY",
    "PuTTY",
}

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


class Win32TargetManager:
    """Manages foreground window tracking and verified paste injection on Windows."""

    @property
    def user32(self):
        windll = getattr(ctypes, "windll", None)
        return getattr(windll, "user32", None) if windll else None

    def get_active_target(self) -> Win32TargetWindow:
        u32 = self.user32
        if u32 is None:
            # Fallback if not on real Windows
            return Win32TargetWindow(
                window_id="0",
                wm_class="Unknown",
                title="Unknown Window",
                is_terminal=False,
            )

        hwnd = u32.GetForegroundWindow()
        title_buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title_buf, 512)
        title = title_buf.value or "Untitled"

        class_buf = ctypes.create_unicode_buffer(512)
        u32.GetClassNameW(hwnd, class_buf, 512)
        cls_name = class_buf.value or "Unknown"

        is_term = (
            cls_name in WIN_TERMINAL_CLASSES
            or "cmd.exe" in title.lower()
            or "powershell" in title.lower()
        )

        return Win32TargetWindow(
            window_id=str(hwnd),
            wm_class=cls_name,
            title=title,
            is_terminal=is_term,
        )

    def verify_active_target(self, expected: Win32TargetWindow) -> bool:
        current = self.get_active_target()
        return current.window_id == expected.window_id

    def paste(self, target: Win32TargetWindow) -> dict:
        """
        Verify target focus and inject Ctrl+V (or Ctrl+Shift+V for console).
        NEVER synthesizes Enter or Return.
        """
        if not self.verify_active_target(target):
            raise TargetMismatchError(
                f"Active window switched! Expected {target.wm_class} (win:{target.window_id}), "
                f"found {self.get_active_target().wm_class}"
            )

        u32 = self.user32
        if u32 is None:
            return {"status": "mock_pasted", "shortcut": "ctrl+v"}

        hwnd = int(target.window_id)
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.05)

        # Synthesize Ctrl+V
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        u32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.02)
        u32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

        return {"status": "pasted", "shortcut": "ctrl+v"}
