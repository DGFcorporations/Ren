"""Wren Windows Clipboard Manager: Non-clobbering clipboard operations on Windows."""
import hashlib
import time
from dataclasses import dataclass


@dataclass
class ClipSnapshot:
    content: str
    content_hash: str
    timestamp: float


class WindowsClipboard:
    """Manages clipboard state, hashing, and safe restore on Windows."""

    def __init__(self):
        try:
            import pyperclip
            self._pyperclip = pyperclip
        except ImportError:
            self._pyperclip = None

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def snapshot(self) -> ClipSnapshot:
        """Capture current clipboard content and SHA256 hash."""
        text = ""
        if self._pyperclip:
            try:
                text = self._pyperclip.paste() or ""
            except Exception:
                text = ""
        return ClipSnapshot(
            content=text,
            content_hash=self._hash(text),
            timestamp=time.time(),
        )

    def write_and_verify(self, text: str) -> str:
        """Write text to clipboard and verify by reading back immediately."""
        if not self._pyperclip:
            raise RuntimeError("pyperclip is required for Windows clipboard operations")

        self._pyperclip.copy(text)
        readback = self._pyperclip.paste()
        if readback != text:
            raise RuntimeError("Clipboard readback verification mismatch on Windows!")

        return self._hash(text)

    def restore_safe(self, prior_snapshot: ClipSnapshot, expected_current_hash: str) -> bool:
        """
        Restore prior snapshot only if the clipboard hasn't been modified
        by an external user action in the interim.
        """
        current_snap = self.snapshot()
        if current_snap.content_hash != expected_current_hash:
            return False

        if self._pyperclip:
            self._pyperclip.copy(prior_snapshot.content)
            return True
        return False
