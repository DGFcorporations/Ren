"""Workspace file tools for Ren. Writes stay inside one root and skip secrets."""

from __future__ import annotations

import os
import time
from pathlib import Path

MAX_BYTES = 200_000
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "build", "dist", ".gradle"}
REFUSED_NAMES = {".env", ".secrets", "id_rsa", "id_ed25519", "credentials.json"}


class WorkspaceError(ValueError):
    pass


class WorkspaceWriter:
    def __init__(self, root: Path | None = None):
        env = os.environ.get("WREN_WORKSPACE")
        self.root = Path(env).resolve() if env else (root or Path.cwd()).resolve()
        self._pending: dict[str, dict] = {}

    def _resolve(self, rel: str) -> Path:
        raw = (rel or "").strip()
        if not raw or raw.startswith("~"):
            raise WorkspaceError("Path is required")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError("Path is outside the Ren workspace")
        rel_parts = () if resolved == self.root else resolved.relative_to(self.root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            raise WorkspaceError("Refusing secrets or dependency directories")
        if resolved.name in REFUSED_NAMES or resolved.name.startswith(".env"):
            raise WorkspaceError("Refusing secrets or dependency directories")
        if resolved.suffix.lower() in {".pem", ".key", ".p12", ".keystore"}:
            raise WorkspaceError("Refusing key material")
        return resolved

    def list_dir(self, rel: str = ".") -> dict:
        target = self._resolve(rel if rel else ".")
        if target.is_file():
            return {"root": str(self.root), "path": str(target), "files": [target.name]}
        if not target.exists():
            raise WorkspaceError("Path does not exist")
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if name in REFUSED_NAMES or name.startswith(".env"):
                    continue
                full = Path(dirpath) / name
                files.append(str(full.relative_to(self.root)))
                if len(files) >= 200:
                    return {"root": str(self.root), "path": rel or ".", "files": files, "truncated": True}
        return {"root": str(self.root), "path": rel or ".", "files": sorted(files), "truncated": False}

    def read(self, rel: str) -> dict:
        path = self._resolve(rel)
        if not path.is_file():
            raise WorkspaceError("Not a file")
        if path.stat().st_size > MAX_BYTES:
            raise WorkspaceError("File is too large")
        return {"path": str(path.relative_to(self.root)), "content": path.read_text(encoding="utf-8", errors="replace")}

    def propose_write(self, rel: str, content: str) -> dict:
        if not isinstance(content, str):
            raise WorkspaceError("Content must be text")
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise WorkspaceError("Content is too large")
        path = self._resolve(rel)
        approval_id = os.urandom(8).hex()
        self._pending[approval_id] = {
            "path": path,
            "rel": str(path.relative_to(self.root)) if path != self.root else rel,
            "content": content,
            "expires": time.time() + 120,
        }
        return {
            "type": "write_preview",
            "approval_id": approval_id,
            "path": str(path.relative_to(self.root)),
            "bytes": len(content.encode("utf-8")),
            "committed": False,
            "requires_confirm": True,
        }

    def commit_write(self, approval_id: str) -> dict:
        item = self._pending.get(approval_id)
        if not item:
            raise WorkspaceError("Unknown write approval")
        if item["expires"] < time.time():
            self._pending.pop(approval_id, None)
            raise WorkspaceError("Write approval expired")
        path: Path = item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")
        self._pending.pop(approval_id, None)
        return {"type": "written", "path": item["rel"], "bytes": len(item["content"].encode("utf-8")), "committed": True}

    def write(self, rel: str, content: str, confirm: bool = False) -> dict:
        proposal = self.propose_write(rel, content)
        if not confirm:
            return proposal
        return self.commit_write(proposal["approval_id"])
