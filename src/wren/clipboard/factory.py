"""Platform factory for Clipboard Managers."""
import sys

from wren.clipboard.windows import WindowsClipboard
from wren.clipboard.x11 import X11Clipboard


def get_clipboard_manager():
    if sys.platform == "win32":
        return WindowsClipboard()
    return X11Clipboard()
