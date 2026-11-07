"""X11 clipboard management using xclip with integrity verification and safe restore."""
import hashlib
import subprocess
from dataclasses import dataclass


class ClipboardError(RuntimeError):
    """Raised when clipboard operations or verification fail."""
    pass


@dataclass
class ClipSnapshot:
    content: str
    content_hash: str


class X11Clipboard:
    """Manages the X11 clipboard selection via xclip."""

    def __init__(self):
        pass

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def snapshot(self) -> ClipSnapshot:
        """Capture the current clipboard content and hash without logging secrets."""
        try:
            res = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            content = res.stdout if res.returncode == 0 else ""
        except Exception:
            content = ""
        return ClipSnapshot(content=content, content_hash=self.hash_text(content))

    def write(self, text: str) -> str:
        """Write text to the clipboard and independently read back to verify."""
        expected_hash = self.hash_text(text)
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input=text,
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            )
        except Exception as e:
            raise ClipboardError(f"Failed to write to X11 clipboard: {e}") from e

        # Independent readback verification
        try:
            read_res = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            read_text = read_res.stdout
        except Exception as e:
            raise ClipboardError(f"Failed to read back from X11 clipboard: {e}") from e

        read_hash = self.hash_text(read_text)
        if read_hash != expected_hash:
            raise ClipboardError(
                f"Readback verification failed: expected {expected_hash[:12]}, got {read_hash[:12]}"
            )
        return expected_hash

    def safe_restore(self, prior_snapshot: ClipSnapshot, wren_written_text: str) -> bool:
        """
        Safely restores the prior clipboard ONLY IF current clipboard still equals
        Wren's written text. Never clobbers content copied externally by the user.
        """
        current_snap = self.snapshot()
        wren_hash = self.hash_text(wren_written_text)
        
        # If the user copied something else in the meantime, preserve their copy!
        if current_snap.content_hash != wren_hash:
            return False

        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-i"],
                input=prior_snapshot.content,
                text=True,
                capture_output=True,
                timeout=3,
                check=True,
            )
            return True
        except Exception:
            return False
