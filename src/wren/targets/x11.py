"""X11 active window tracking, identity hashing, focus verification, and safe paste injection."""
import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass


class TargetMismatchError(RuntimeError):
    """Raised when active window does not match the approved target."""
    pass


TERMINAL_CLASSES = {
    "gnome-terminal",
    "x-terminal-emulator",
    "tilix",
    "alacritty",
    "kitty",
    "terminator",
    "konsole",
    "xfce4-terminal",
}


@dataclass
class TargetWindow:
    window_id: str
    wm_class: str
    title: str
    pid: int
    target_hash: str
    is_terminal: bool = False
    is_self: bool = False


class X11TargetManager:
    """Detects and validates active X11 windows before paste execution."""

    # Window title of the Ren widget; PID match is authoritative, title is the fallback.
    SELF_WINDOW_TITLE = "Wren Assistant"

    def __init__(self):
        self._last_external: TargetWindow | None = None

    @staticmethod
    def compute_hash(window_id: str, wm_class: str, title: str, pid: int) -> str:
        raw = f"{window_id}:{wm_class}:{title}:{pid}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_active_target(self) -> TargetWindow:
        """Query active window ID, WM_CLASS, window title, and process PID via xdotool and xprop."""
        try:
            win_res = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            window_id = win_res.stdout.strip()
        except Exception as e:
            raise RuntimeError(f"Could not retrieve active X11 window: {e}") from e

        # Get WM_CLASS
        wm_class = "unknown"
        try:
            prop_res = subprocess.run(
                ["xprop", "-id", window_id, "WM_CLASS"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            # WM_CLASS(STRING) = "gedit", "Gedit"
            match = re.search(r'=\s*"([^"]+)"', prop_res.stdout)
            if match:
                wm_class = match.group(1).lower()
        except Exception:
            pass

        # Get window title
        title = ""
        try:
            title_res = subprocess.run(
                ["xdotool", "getwindowname", window_id],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            title = title_res.stdout.strip()
        except Exception:
            pass

        # Get window PID
        pid = 0
        try:
            pid_res = subprocess.run(
                ["xdotool", "getwindowpid", window_id],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            pid = int(pid_res.stdout.strip())
        except Exception:
            pass

        is_term = wm_class in TERMINAL_CLASSES or "terminal" in wm_class
        is_self = (pid != 0 and pid == os.getpid()) or title.strip() == self.SELF_WINDOW_TITLE
        thash = self.compute_hash(window_id, wm_class, title, pid)

        return TargetWindow(
            window_id=window_id,
            wm_class=wm_class,
            title=title,
            pid=pid,
            target_hash=thash,
            is_terminal=is_term,
            is_self=is_self,
        )

    def get_paste_target(self) -> TargetWindow:
        """The window Ren should paste into: the active app, never Ren's own widget.

        When the Ren orb holds focus, returns the most recent external window so
        dictation always has a direction. Raises if no external window was ever seen.
        """
        current = self.get_active_target()
        if getattr(current, "is_self", False):
            if self._last_external is not None:
                return self._last_external
            raise RuntimeError("No external target recorded yet — focus another app first")
        self._last_external = current
        return current

    def peek_paste_target(self) -> TargetWindow | None:
        """Best-known paste target without raising; refreshes the external memory."""
        try:
            current = self.get_active_target()
            if not getattr(current, "is_self", False):
                self._last_external = current
                return current
        except Exception:
            pass
        return self._last_external

    def verify_target(self, expected_target: TargetWindow) -> bool:
        """Verify that current foreground window matches the recorded target window."""
        try:
            current = self.get_active_target()
            return current.window_id == expected_target.window_id
        except Exception:
            return False

    def _activate_window(self, window_id: str) -> None:
        """Raise and focus a window so real (XTEST) input reaches it."""
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", window_id],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )

    def paste_into_target(self, expected_target: TargetWindow) -> dict[str, str]:
        """
        Synthesizes paste key combination into the target window.
        NEVER sends Enter or Return!

        If the Ren widget itself holds focus (user clicked Paste inside it),
        the approved target window is activated first, then real keys are sent.
        """
        current = self.get_active_target()
        self_focused = getattr(current, "is_self", False)
        if not self_focused and current.window_id != expected_target.window_id:
            raise TargetMismatchError(
                f"Active window changed from {expected_target.wm_class} ({expected_target.window_id}) "
                f"to {current.wm_class} ({current.window_id}). Aborting paste."
            )

        key_combo = "ctrl+shift+v" if expected_target.is_terminal else "ctrl+v"

        try:
            if self_focused:
                self._activate_window(expected_target.window_id)
                time.sleep(0.1)
                subprocess.run(
                    ["xdotool", "key", key_combo],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
            else:
                subprocess.run(
                    ["xdotool", "key", "--window", expected_target.window_id, key_combo],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
        except Exception as e:
            raise RuntimeError(f"xdotool paste injection failed: {e}") from e

        return {
            "status": "pasted",
            "window_id": expected_target.window_id,
            "wm_class": expected_target.wm_class,
            "shortcut": key_combo,
        }
