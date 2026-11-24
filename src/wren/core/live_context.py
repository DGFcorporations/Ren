"""Live multimodal context & conversation engine for Ren and MCP agents.

Captures a unified live snapshot of:
1. Current workspace files (active/recently modified files or explicit paths)
2. Desktop app interfaces (active window, open app/chat windows, optional PNG screenshot)
3. Shared live conversation history accessible to both the user (HTTP/WebSocket) and MCP agents.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pilot-data", ".pytest_cache"}
MAX_FILE_BYTES = 12_000
MAX_RECENT_FILES = 8


class LiveContextError(RuntimeError):
    """Raised when live context capture or conversation fails."""


@dataclass
class ConversationTurn:
    role: str
    text: str
    timestamp: float
    files_seen: list[str] = field(default_factory=list)
    active_window: str = ""
    had_screenshot: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class LiveConverseRequest(BaseModel):
    text: str
    role: str = "user"
    include_screenshot: bool = False
    include_files: bool = True
    include_windows: bool = True
    file_paths: list[str] = Field(default_factory=list)


class LiveContextSnapshotter:
    """Gathers current workspace files, desktop app interfaces, and screen captures."""

    def __init__(self, workspace_root: Path | None = None, chat_controller: Any = None):
        raw_root = os.environ.get("WREN_WORKSPACE", ".")
        self.workspace = (workspace_root or Path(raw_root)).expanduser().resolve()
        self._chat = chat_controller

    def _get_chat(self):
        if self._chat is None:
            try:
                from wren.targets.chat_windows import DesktopChatController

                self._chat = DesktopChatController()
            except Exception:
                # Fallback for testing environments or unavailable desktop targets
                class MockChat:
                    def list_windows(self, chat_only=False):
                        return []

                self._chat = MockChat()
        return self._chat

    def _safe_resolve(self, rel_path: str) -> Path:
        candidate = (self.workspace / rel_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise LiveContextError(f"Path escapes WREN_WORKSPACE: {rel_path}")
        return candidate

    def read_current_files(self, file_paths: list[str] | None = None) -> list[dict]:
        """Read explicit file paths or the most recently modified workspace files."""
        targets: list[Path] = []
        if file_paths:
            for raw in file_paths[:MAX_RECENT_FILES]:
                try:
                    resolved = self._safe_resolve(raw)
                    if resolved.is_file():
                        targets.append(resolved)
                except (LiveContextError, OSError):
                    continue
        else:
            targets = self._discover_recent_files(limit=MAX_RECENT_FILES)

        snapshots: list[dict] = []
        for path in targets:
            snapshots.append(self._read_file_preview(path))
        return snapshots

    def _discover_recent_files(self, limit: int) -> list[Path]:
        candidates: list[tuple[float, Path]] = []
        if not self.workspace.exists():
            return []
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                p = Path(root) / name
                try:
                    candidates.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in candidates[:limit]]

    def _read_file_preview(self, path: Path) -> dict:
        try:
            rel = str(path.relative_to(self.workspace))
        except ValueError:
            rel = path.name
        try:
            raw = path.read_bytes()
            truncated = len(raw) > MAX_FILE_BYTES
            text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
            return {
                "path": rel,
                "size_bytes": len(raw),
                "truncated": truncated,
                "content": text,
            }
        except OSError as exc:
            return {"path": rel, "error": str(exc), "content": ""}

    def read_desktop_interfaces(self) -> dict:
        """Inspect the active window and list open desktop app/chat interfaces."""
        active = self._read_active_window()
        try:
            windows = self._get_chat().list_windows(chat_only=False)
        except Exception:
            windows = []
        return {
            "active_window": active,
            "open_windows": windows[:25],
            "chat_windows": [w for w in windows if w.get("is_chat_app")][:15],
        }

    @staticmethod
    def _read_active_window() -> dict:
        if not shutil.which("xdotool"):
            return {"window_id": "", "title": "", "wm_class": ""}
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            if not wid:
                return {"window_id": "", "title": "", "wm_class": ""}
            title = subprocess.run(
                ["xdotool", "getwindowname", wid],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            return {"window_id": wid, "title": title, "wm_class": ""}
        except Exception:
            return {"window_id": "", "title": "", "wm_class": ""}

    @staticmethod
    def capture_screenshot_b64() -> str:
        """Capture the current desktop screen as a base64-encoded PNG."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            for cmd in (
                ["scrot", "-o", str(tmp_path)],
                ["gnome-screenshot", "-f", str(tmp_path)],
                ["import", "-window", "root", str(tmp_path)],
            ):
                if shutil.which(cmd[0]):
                    res = subprocess.run(cmd, capture_output=True, timeout=5, check=False)
                    if res.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                        return base64.b64encode(tmp_path.read_bytes()).decode("ascii")
            return ""
        finally:
            tmp_path.unlink(missing_ok=True)

    def capture(
        self,
        *,
        include_files: bool = True,
        include_windows: bool = True,
        include_screenshot: bool = False,
        file_paths: list[str] | None = None,
    ) -> dict:
        files = self.read_current_files(file_paths) if include_files else []
        interfaces = self.read_desktop_interfaces() if include_windows else {}
        screenshot_b64 = self.capture_screenshot_b64() if include_screenshot else ""
        return {
            "type": "live_context",
            "timestamp": time.time(),
            "workspace": str(self.workspace),
            "files": files,
            "desktop": interfaces,
            "has_screenshot": bool(screenshot_b64),
            "screenshot_b64": screenshot_b64,
        }


class LiveConversationEngine:
    """Manages live multimodal turns grounded in current files and desktop interfaces."""

    def __init__(self, controller: Any = None, snapshotter: LiveContextSnapshotter | None = None):
        self.controller = controller
        self.snapshotter = snapshotter or LiveContextSnapshotter()
        self.history: list[ConversationTurn] = []

    def get_history(self, limit: int = 30) -> list[dict]:
        return [turn.to_dict() for turn in self.history[-limit:]]

    def clear_history(self) -> dict:
        count = len(self.history)
        self.history.clear()
        return {"cleared": count}

    def converse(
        self,
        text: str,
        *,
        role: str = "user",
        include_screenshot: bool = False,
        include_files: bool = True,
        include_windows: bool = True,
        file_paths: list[str] | None = None,
    ) -> dict:
        prompt = (text or "").strip()
        if not prompt:
            raise LiveContextError("Conversation text cannot be empty")

        snapshot = self.snapshotter.capture(
            include_files=include_files,
            include_windows=include_windows,
            include_screenshot=include_screenshot,
            file_paths=file_paths,
        )
        active_title = (snapshot.get("desktop") or {}).get("active_window", {}).get("title", "")
        files_seen = [f["path"] for f in snapshot.get("files", [])]

        self.history.append(
            ConversationTurn(
                role=role,
                text=prompt,
                timestamp=time.time(),
                files_seen=files_seen,
                active_window=active_title,
                had_screenshot=snapshot["has_screenshot"],
            )
        )

        reply = self._generate_reply(prompt, snapshot)
        self.history.append(
            ConversationTurn(
                role="ren",
                text=reply,
                timestamp=time.time(),
                files_seen=files_seen,
                active_window=active_title,
                had_screenshot=snapshot["has_screenshot"],
            )
        )
        self.history = self.history[-80:]

        return {
            "type": "live_reply",
            "reply": reply,
            "files_seen": files_seen,
            "active_window": active_title,
            "had_screenshot": snapshot["has_screenshot"],
            "history_length": len(self.history),
        }

    def _generate_reply(self, prompt: str, snapshot: dict) -> str:
        # Simple local implementation
        files_label = ", ".join(f["path"] for f in snapshot.get("files", [])) or "none"
        win_title = (snapshot.get("desktop") or {}).get("active_window", {}).get("title") or "unknown"
        return (
            f"Live context ready (active window: {win_title}; files: {files_label}). "
            f"Received: {prompt}"
        )
