"""Platform factory for Target Managers."""
import sys

from wren.targets.win32 import Win32TargetManager
from wren.targets.x11 import X11TargetManager


def get_target_manager():
    if sys.platform == "win32":
        return Win32TargetManager()
    return X11TargetManager()
